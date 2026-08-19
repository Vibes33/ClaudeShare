"""Un salon : un agent hôte, un journal, des abonnés.

Le salon est le point où les briques se rejoignent — un agent distant produit
des événements, le journal les numérote, le diffuseur les distribue. C'est aussi
le seul endroit qui décide : les clients envoient des intentions, jamais des
ordres.

**Le salon n'exécute rien.** La session Claude Code vit chez l'agent, sur la
machine de son propriétaire, avec son abonnement et son dossier de travail. Ce
qui reste ici est de la coordination : qui a la parole, qui a le droit de quoi,
ce qui s'est passé. Un salon sans agent connecté existe, s'affiche et se lit —
il ne peut simplement pas faire tourner de tour.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..agent.hooks import AuditRecord
from ..agent.toolpolicy import TrustLevel
from ..core.broker import Broadcaster, InProcessBroadcaster
from ..core.eventlog import EventLog, LogStore
from ..core.floor import Denial, Floor, Outcome
from ..events import Event, EventType
from ..protocol import EFFORTS, MODELS, ServerMessage, envelope
from .agentlink import AbsentAgent, AgentLink, NoAgentError
from .approvals import ApprovalDesk

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class Submission:
    """Ce qu'il advient d'un prompt soumis."""

    started: bool
    #: Pourquoi il n'est pas parti. Code de `Denial`, stable pour les interfaces.
    reason: str = ""


class Room:
    """Une conversation partagée."""

    def __init__(
        self,
        room_id: str,
        *,
        broker: Broadcaster,
        title: str = "",
        session_id: str | None = None,
        floor: Floor | None = None,
        store: LogStore | None = None,
    ) -> None:
        self.id = room_id
        self.title = title or room_id
        #: Dernière session Claude annoncée par un agent. Conservée pour qu'un
        #: agent qui revient reprenne le bon contexte (`resume`).
        self.session_id = session_id
        # Sans magasin, le journal vit en mémoire et meurt avec le processus.
        # C'est ce qu'on veut pour les tests et une session locale jetable.
        self.log = EventLog(room_id=room_id, store=store)
        self.broker = broker
        self.floor = floor or Floor()
        #: Dernier état du jeton annoncé au salon. Sert à ne diffuser que les
        #: vrais changements — voir `_apply`.
        self._floor_signature = self.floor.signature
        #: Nettoyages en cours. Référencés pour qu'ils ne soient pas ramassés
        #: avant la fin — `create_task` ne garde qu'une référence faible.
        self._chores: set[asyncio.Task[Any]] = set()
        #: Guichet des approbations. Il ne décide pas — le courtier de l'agent
        #: tient la promesse et le délai ; ici on sait seulement qui attend et à
        #: quelle liaison renvoyer la réponse.
        self.approvals = ApprovalDesk(answer=self._answer_approval)
        #: Pseudos actuellement connectés. Une même personne peut avoir plusieurs
        #: onglets, d'où le comptage.
        self._present: dict[str, int] = {}
        #: Photo de profil par étiquette présente. Transportée à côté de la
        #: présence, et non dedans : la liste des présents est lue par la TUI,
        #: qui n'affiche pas d'image et n'a pas à connaître ce champ.
        self._avatars: dict[str, str] = {}
        self._audit: list[AuditRecord] = []
        self._turn: asyncio.Task[Any] | None = None
        #: Modèle et intensité de réflexion demandés depuis l'interface. Ce que
        #: le salon a *demandé*, pas ce que la session applique : seule la
        #: machine de l'hôte le sait, et elle l'annonce par `session.ready`.
        self.config: dict[str, str] = {"model": "", "effort": ""}
        #: Dernier état de quota rapporté par l'agent, ou `None` tant qu'il n'a
        #: rien dit. Le `None` compte : une jauge à zéro se lirait « rien
        #: consommé », ce qui est un mensonge quand on ne sait pas.
        self.quota: dict[str, Any] | None = None
        #: `seq` de la dernière réponse terminée. Sert aux interfaces à savoir
        #: qu'il s'est passé quelque chose *ici* pendant qu'on regardait
        #: ailleurs — d'où `turn.ended` et non `last_seq`, qu'une simple demande
        #: de parole ferait avancer.
        self._last_reply = self._derniere_reponse()
        #: L'agent qui héberge, ou son absence. Un objet plutôt qu'un `None` :
        #: `busy` et `session_id` sont lus par les routes, l'instantané et le
        #: WebSocket, et un test de présence oublié se verrait à l'exécution.
        self.agent: AgentLink | AbsentAgent = AbsentAgent()

    def _derniere_reponse(self) -> int:
        """Retrouve dans le journal le `seq` de la dernière réponse rendue.

        Relu au montage plutôt que reparti de zéro : sans ça, un relais qui
        redémarre effacerait les pastilles de tout le monde — ou pire, les
        allumerait toutes.
        """
        return max(
            (
                int(e.get("seq") or 0)
                for e in self.log.since(0).events
                if e.get("type") == str(EventType.TURN_ENDED)
            ),
            default=0,
        )

    @property
    def last_reply(self) -> int:
        return self._last_reply

    # ------------------------------------------------------------ hébergement

    def host(self, link: AgentLink) -> None:
        """Attache un agent. Remplace le précédent, s'il y en avait un.

        Le remplacement est voulu : après une coupure réseau, l'ancienne socket
        peut mettre longtemps à être déclarée morte, et un propriétaire qui
        relance son agent ne doit pas avoir à attendre ce délai.
        """
        ancien = self.agent
        if isinstance(ancien, AgentLink) and ancien.connected:
            logger.info("agent remplacé sur %s", self.id)
            ancien.dropped()
        self.agent = link

    def unhost(self, link: AgentLink) -> None:
        """Détache un agent, s'il est bien celui qui héberge encore."""
        if self.agent is not link:
            return
        link.dropped()
        self.agent = AbsentAgent()
        # Les demandes en vol ne sont pas refusées ici : seul l'agent peut
        # répondre à la promesse qu'il a faite au SDK, et son délai s'en charge.
        self.approvals.forget()

    @property
    def hosted(self) -> bool:
        return self.agent.connected

    async def _answer_approval(
        self, approval_id: str, *, allow: bool, by: str = "", reason: str = ""
    ) -> None:
        await self.agent.answer(approval_id, allow=allow, by=by, reason=reason)

    async def on_agent_event(self, event: Event) -> None:
        """Événement venu de l'agent : suivi, journalisé, diffusé."""
        self.approvals.observe(event)
        await self._on_event(event)

    # ------------------------------------------------------------ événements

    async def _on_event(self, event: Event) -> None:
        """Journalise puis diffuse. L'ordre compte : le `seq` vient du journal."""
        logged = self.log.append(event)
        if event.type is EventType.RATE_LIMIT:
            self.quota = dict(event.data)
        elif event.type is EventType.TURN_ENDED and logged is not None:
            self._last_reply = logged.seq
        await self.broker.publish(
            self.id,
            envelope(
                event.type,
                self.id,
                event.payload(),
                seq=logged.seq if logged else None,
            ),
        )

    async def _on_audit(self, record: AuditRecord) -> None:
        self._audit.append(record)

    @property
    def audit(self) -> list[AuditRecord]:
        return list(self._audit)

    # -------------------------------------------------------------- présence

    async def joined(self, who: str, avatar: str | None = None) -> None:
        self._present[who] = self._present.get(who, 0) + 1
        if avatar:
            self._avatars[who] = avatar
        if self._present[who] == 1:
            await self._announce()

    def departure(self, who: str) -> None:
        """Planifie le nettoyage d'un départ, hors de la connexion qui se ferme.

        Une connexion qui part n'est pas un support fiable pour son propre
        nettoyage : sa tâche peut être annulée dès la trame de fermeture, et
        tout `await` placé après ne reprendrait jamais — la présence resterait
        alors affichée et le jeton réservé à quelqu'un qui n'est plus là. Le
        salon, lui, survit à ses connexions.
        """
        self.schedule(self.left(who))

    def schedule(self, coro: Any) -> None:
        """Confie un travail au salon, qui survit aux connexions.

        Référencé le temps qu'il tourne : `create_task` ne garde qu'une
        référence faible, et une tâche ramassée en plein vol disparaît sans
        rien dire.
        """
        tache = asyncio.create_task(coro)
        self._chores.add(tache)
        tache.add_done_callback(self._chores.discard)

    async def left(self, who: str) -> None:
        remaining = self._present.get(who, 1) - 1
        if remaining > 0:
            self._present[who] = remaining
            return
        self._present.pop(who, None)
        # Le dernier onglet fermé libère le jeton : le garder réservé à
        # quelqu'un qui est parti bloque tout le monde pour rien.
        await self._apply(self.floor.depart(who))
        await self._announce()

    @property
    def present(self) -> list[str]:
        return sorted(self._present)

    @property
    def avatars(self) -> dict[str, str]:
        """Les photos des seules personnes présentes.

        Filtré ici plutôt qu'au départ de chacun : une étiquette peut revenir,
        et garder son image évite de la redemander à chaque reconnexion.
        """
        return {qui: url for qui, url in self._avatars.items() if qui in self._present}

    async def _announce(self) -> None:
        await self.broker.publish(
            self.id,
            envelope(
                ServerMessage.PRESENCE,
                self.id,
                {"present": self.present, "avatars": self.avatars},
            )
        )

    async def announce_agent(self) -> None:
        """Annonce l'arrivée ou le départ de l'hôte.

        Un état, comme la présence, donc jamais journalisé : ce qui compte est
        de savoir si le salon est exécutable *maintenant*, pas de conserver
        l'histoire de ses coupures réseau.
        """
        await self.broker.publish(
            self.id, envelope(ServerMessage.AGENT, self.id, self.agent.view())
        )

    # ---------------------------------------------------------------- tours

    def snapshot(self, last_seq: int = 0) -> dict[str, Any]:
        """État à envoyer à un client qui (re)connecte.

        Les `partials` sont à **remplacer** côté client, pas à concaténer : sans
        ça, se reconnecter en plein tour duplique le texte déjà reçu.
        """
        rejeu = self.log.since(last_seq)
        return envelope(
            ServerMessage.SNAPSHOT,
            self.id,
            {
                "title": self.title,
                "last_seq": self.log.last_seq,
                "events": rejeu.events,
                # Le début de l'historique manque. Dit explicitement, parce
                # qu'une conversation qui commence au milieu sans le signaler est
                # exactement le trou silencieux qu'on passe le reste du protocole
                # à éviter.
                "truncated": rejeu.truncated,
                "partials": self.log.partials(),
                "present": self.present,
                "avatars": self.avatars,
                "busy": self.agent.busy,
                "session_id": self.agent.session_id or self.session_id,
                # Qui héberge, et depuis quel dossier. Un salon sans agent
                # s'affiche quand même : on peut lire l'historique, on ne peut
                # pas soumettre. Le dire est plus utile qu'un envoi qui échoue.
                "agent": self.agent.view(),
                "floor": self.floor.view(),
                # Ce qui a été demandé, et ce qui est proposable. Les listes
                # viennent du serveur : les redire dans le JavaScript ferait
                # deux vocabulaires à tenir d'accord pour un menu déroulant.
                "config": dict(self.config),
                "options": {"models": list(MODELS), "efforts": list(EFFORTS)},
                # `None` tant que l'agent n'a rien rapporté — voir `quota`.
                "quota": self.quota,
                # Sans ça, arriver pendant une demande d'approbation montrerait
                # un tour figé sans dire pourquoi.
                "approvals": self.approvals.pending(),
            },
            seq=self.log.last_seq,
        )

    async def submit(
        self,
        prompt: str,
        author: str,
        trust: TrustLevel = TrustLevel.WRITER,
        priority: int = 0,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Submission:
        """Lance un tour, ou met la personne en file.

        Envoyer un prompt vaut demande de parole : quelqu'un qui est seul dans
        un salon n'a aucune raison de réclamer un jeton avant d'écrire.

        On ne bloque pas l'appelant : le tour dure des minutes, et sa
        progression arrive à tout le monde par la diffusion, pas par cette
        réponse.

        Lève `NoAgentError` si personne n'héberge. Vérifié **avant** de toucher
        au jeton : prendre la parole pour découvrir ensuite qu'aucun agent
        n'écoute laisserait le salon bloqué le temps de l'expiration.
        """
        if not self.hosted:
            raise NoAgentError(
                "aucun agent n'héberge ce salon — son propriétaire doit lancer "
                "`claudeshare agent`"
            )

        # Soumettre ne demande plus la parole : il faut l'avoir. Le faire ici
        # revenait à servir le premier arrivé, et c'est exactement ce que le
        # jeton sur approbation supprime. Une demande est une intention à part,
        # que quelqu'un doit accorder — voir `core/floor.py`.
        if not self.floor.can_send(author):
            return Submission(started=False, reason=str(self._blocage(author)))

        await self._apply(self.floor.begin_turn(author))

        async def run() -> None:
            try:
                # Le niveau de confiance voyage avec le tour : c'est l'agent qui
                # l'applique, sur la machine qui a quelque chose à perdre si un
                # invité obtient un outil de trop.
                await self.agent.run_turn(
                    prompt, author=author, trust=trust, attachments=attachments or []
                )
            except Exception:
                logger.exception("le tour a échoué dans %s", self.id)
            finally:
                # Envoyer libère : le jeton repart à la file. Sans ce `finally`,
                # un tour qui échoue laisserait le salon bloqué en `generating`.
                await self._apply(self.floor.end_turn())

        self._turn = asyncio.create_task(run())
        return Submission(started=True)

    async def say(self, who: str, texte: str) -> None:
        """Dit quelque chose aux humains du salon.

        Ne touche ni au jeton de parole ni à l'agent : c'est une conversation
        parallèle, qui doit rester possible **pendant** qu'un tour tourne — sinon
        elle ne servirait à rien, puisque c'est justement le moment où l'on veut
        se dire quelque chose sans couper la réponse.
        """
        await self._on_event(
            Event(type=EventType.CHAT_MESSAGE, author=who, data={"text": texte})
        )

    async def configure(
        self, *, who: str, model: str | None = None, effort: str | None = None
    ) -> None:
        """Change le modèle ou l'intensité de réflexion de la session.

        Le relais valide contre ses propres listes avant de transmettre : ce qui
        part d'ici finit en drapeau de ligne de commande sur la machine de
        quelqu'un, et une valeur libre venue d'un navigateur n'a rien à y faire.

        L'événement est émis même sans agent connecté : le réglage est une
        propriété du salon, et il s'appliquera à la prochaine session.
        """
        if model is not None:
            if model not in MODELS:
                raise ValueError(f"modèle inconnu : {model!r}")
            self.config["model"] = model
        if effort is not None:
            if effort not in EFFORTS:
                raise ValueError(f"intensité inconnue : {effort!r}")
            self.config["effort"] = effort

        await self.agent.configure(model=model, effort=effort)
        await self._on_event(
            Event(type=EventType.SESSION_CONFIG, author=who, data=dict(self.config))
        )

    # ------------------------------------------------------- jeton de parole

    def _blocage(self, who: str) -> Denial:
        """Pourquoi cette personne ne peut pas envoyer. Pour le dire, pas pour décider.

        Trois empêchements différents, trois messages différents : ne pas avoir
        la parole, l'avoir mais attendre la fin d'un tour, ou l'avoir obtenue
        pour la fin du tour en cours. Les confondre sous « vous n'avez pas la
        parole » ferait chercher un droit à quelqu'un qui n'a qu'à patienter.
        """
        if self.floor.holder == who:
            return Denial.TURN_RUNNING
        return Denial.NOT_HOLDER

    async def request_floor(self, who: str, priority: int = 0) -> Outcome:
        """Demande la parole. Ne l'accorde pas : quelqu'un doit trancher."""
        return await self._apply(self.floor.request(who, priority))

    async def withdraw_floor(self, who: str) -> Outcome:
        return await self._apply(self.floor.withdraw(who))

    async def grant_floor(self, who: str, *, immediate: bool = False) -> Outcome:
        """Accorde la parole. L'appelant a vérifié `room.floor.grant`.

        `immediate` coupe le tour en cours et demande en plus `room.preempt` :
        attendre la fin d'un tour est le comportement, l'interrompre est
        l'exception.
        """
        return await self._apply(self.floor.grant(who, immediate=immediate))

    async def deny_floor(self, who: str) -> Outcome:
        return await self._apply(self.floor.deny(who))

    async def revoke_floor(self) -> Outcome:
        return await self._apply(self.floor.revoke())

    async def release_floor(self, who: str) -> Outcome:
        return await self._apply(self.floor.release(who))

    async def _apply(self, outcome: Outcome) -> Outcome:
        """Exécute les conséquences d'une transition du jeton.

        La machine à états ne fait que décider ; couper réellement le tour et
        prévenir le salon se passe ici. C'est ce partage qui permet de tester
        l'ordonnancement sans réseau ni horloge réelle.
        """
        if outcome.interrupt:
            # Le drainage du tampon est assuré par `interrupt()` : sans lui, les
            # messages du tour coupé se mélangeraient au tour suivant.
            await self.agent.interrupt()

        # On diffuse sur **ce qui a changé de visible**, pas sur ce que la
        # transition prétend avoir fait. Voir `Floor.signature` : c'est la seule
        # formulation qu'on ne peut pas oublier d'appliquer en ajoutant un cas.
        if (signature := self.floor.signature) != self._floor_signature:
            self._floor_signature = signature
            await self._on_event(
                Event(
                    type=EventType.FLOOR_CHANGED,
                    author=outcome.granted or outcome.revoked,
                    data={"reason": outcome.reason, **self.floor.view()},
                )
            )
        return outcome

    # ------------------------------------------------------------ cycle de vie

    async def start(self) -> None:
        """Démarre la coordination. N'attend aucun agent.

        Un salon existe sans hôte : on peut y lire l'historique et demander la
        parole. Attendre un agent pour ouvrir le salon rendrait la conversation
        illisible dès que son propriétaire ferme son portable.

        Plus rien à lancer ici depuis que le jeton n'expire plus : il n'y a
        aucune échéance à faire passer. La méthode reste — le cycle de vie d'un
        salon est appelé de plusieurs endroits, et le rendre asymétrique pour
        gagner quatre lignes ferait chercher où est passé le `start`.
        """

    async def stop(self) -> bool:
        return await self.agent.interrupt()

    async def aclose(self) -> None:
        if self._chores:
            await asyncio.gather(*self._chores, return_exceptions=True)
        if self._turn is not None and not self._turn.done():
            await self.agent.interrupt()
        self.approvals.forget()


class RoomManager:
    """Salons vivants du process.

    « Vivants » et non « actifs » : depuis que l'exécution est partie chez les
    agents, un salon monté ici ne consomme qu'un journal et un jeton de parole.
    C'est ce qui rend l'épinglage au process beaucoup moins coûteux qu'avant —
    il reste vrai, mais ce qu'on épingle ne pèse plus rien.
    """

    def __init__(
        self, broker: Broadcaster | None = None, *, store: LogStore | None = None
    ) -> None:
        self.broker = broker or InProcessBroadcaster()
        #: Persistance du journal, partagée par tous les salons du process.
        self.store = store
        self._rooms: dict[str, Room] = {}

    def create(self, room_id: str, **kwargs: Any) -> Room:
        if room_id in self._rooms:
            raise ValueError(f"le salon {room_id} existe déjà")
        kwargs.setdefault("store", self.store)
        room = Room(room_id, broker=self.broker, **kwargs)
        self._rooms[room_id] = room
        return room

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def forget(self, room_id: str) -> None:
        """Retire un salon des salons vivants.

        Appelé après un archivage : le laisser monté garderait son journal et
        son jeton de parole en mémoire pour une conversation que plus personne
        ne peut ouvrir.
        """
        self._rooms.pop(room_id, None)

    def list(self) -> list[Room]:
        return list(self._rooms.values())

    async def aclose(self) -> None:
        for room in self._rooms.values():
            await room.aclose()
        self._rooms.clear()
