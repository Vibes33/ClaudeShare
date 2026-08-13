"""Un salon : une session Claude Code, un journal, des abonnés.

Le salon est le point où les trois briques se rejoignent — le superviseur
produit des événements, le journal les numérote, le diffuseur les distribue.
C'est aussi le seul endroit qui décide : les clients envoient des intentions,
jamais des ordres.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent import SessionSupervisor
from ..agent.approval import ApprovalBroker
from ..agent.hooks import AuditRecord
from ..agent.toolpolicy import READ_ONLY_TOOLS, TrustLevel
from ..core.broker import InProcessBroadcaster
from ..core.eventlog import EventLog
from ..core.floor import Floor, Outcome
from ..events import Event, EventType
from ..protocol import ServerMessage, envelope

logger = logging.getLogger(__name__)

#: Fréquence de vérification des échéances du jeton. Le jeton n'a pas besoin
#: d'expirer à la seconde près ; sonder plus souvent ne ferait que réveiller la
#: boucle pour rien.
TICK_INTERVAL_S = 5.0


@dataclass(frozen=True, slots=True)
class Submission:
    """Ce qu'il advient d'un prompt soumis."""

    started: bool
    #: Rang dans la file quand le tour n'a pas pu démarrer.
    position: int | None = None


class Room:
    """Une conversation partagée."""

    def __init__(
        self,
        room_id: str,
        *,
        workspace: Path,
        broker: InProcessBroadcaster,
        title: str = "",
        trust: TrustLevel = TrustLevel.PILOT,
        sandbox: bool = True,
        session_id: str | None = None,
        supervisor: SessionSupervisor | None = None,
        floor: Floor | None = None,
        approval_timeout: float | None = None,
    ) -> None:
        self.id = room_id
        self.title = title or room_id
        self.workspace = workspace
        self.log = EventLog()
        self.broker = broker
        self.floor = floor or Floor()
        #: Dernier état du jeton annoncé au salon. Sert à ne diffuser que les
        #: vrais changements — voir `_apply`.
        self._floor_signature = self.floor.signature
        self._ticker: asyncio.Task[Any] | None = None
        #: Nettoyages en cours. Référencés pour qu'ils ne soient pas ramassés
        #: avant la fin — `create_task` ne garde qu'une référence faible.
        self._chores: set[asyncio.Task[Any]] = set()
        # Créé avant le superviseur : sa fermeture capture `self.agent`, qui
        # n'existe pas encore mais existera avant le premier appel d'outil.
        self.approvals = ApprovalBroker(
            sink=self._on_event,
            context=lambda: (self.agent.current_author, self.agent.current_turn),
            **({"timeout": approval_timeout} if approval_timeout else {}),
        )
        #: Pseudos actuellement connectés. Une même personne peut avoir plusieurs
        #: onglets, d'où le comptage.
        self._present: dict[str, int] = {}
        self._audit: list[AuditRecord] = []
        self._turn: asyncio.Task[Any] | None = None
        #: Niveau de confiance de l'auteur du tour en cours. Les options du SDK
        #: étant fixées à l'ouverture de la session, c'est par ce portail que la
        #: politique par auteur s'applique — via le hook, à chaque appel d'outil.
        self._turn_trust: TrustLevel = TrustLevel.WRITER
        self.agent = supervisor or SessionSupervisor(
            workspace=workspace,
            sink=self._on_event,
            trust=trust,
            sandbox=sandbox,
            session_id=session_id,
            audit=self._on_audit,
            tools_gate=self._tools_gate,
            can_use_tool=self.approvals.ask,
            shared=True,
        )

    def _tools_gate(self) -> frozenset[str] | None:
        """Outils permis pour le tour en cours, selon son auteur."""
        if self._turn_trust is TrustLevel.READER:
            return frozenset(READ_ONLY_TOOLS)
        return None

    # ------------------------------------------------------------ événements

    async def _on_event(self, event: Event) -> None:
        """Journalise puis diffuse. L'ordre compte : le `seq` vient du journal."""
        logged = self.log.append(event)
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

    async def joined(self, who: str) -> None:
        self._present[who] = self._present.get(who, 0) + 1
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
        tache = asyncio.create_task(self.left(who))
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

    async def _announce(self) -> None:
        await self.broker.publish(
            self.id, envelope("presence", self.id, {"present": self.present})
        )

    # ---------------------------------------------------------------- tours

    def snapshot(self, last_seq: int = 0) -> dict[str, Any]:
        """État à envoyer à un client qui (re)connecte.

        Les `partials` sont à **remplacer** côté client, pas à concaténer : sans
        ça, se reconnecter en plein tour duplique le texte déjà reçu.
        """
        return envelope(
            ServerMessage.SNAPSHOT,
            self.id,
            {
                "title": self.title,
                "last_seq": self.log.last_seq,
                "events": [e.to_dict() for e in self.log.since(last_seq)],
                "partials": self.log.partials(),
                "present": self.present,
                "busy": self.agent.busy,
                "session_id": self.agent.session_id,
                "floor": self.floor.view(),
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
    ) -> Submission:
        """Lance un tour, ou met la personne en file.

        Envoyer un prompt vaut demande de parole : quelqu'un qui est seul dans
        un salon n'a aucune raison de réclamer un jeton avant d'écrire.

        On ne bloque pas l'appelant : le tour dure des minutes, et sa
        progression arrive à tout le monde par la diffusion, pas par cette
        réponse.
        """
        demande = await self._apply(self.floor.request(author, priority))
        if self.floor.holder != author:
            return Submission(started=False, position=demande.position)

        await self._apply(self.floor.begin_turn(author))
        self._turn_trust = trust

        async def run() -> None:
            try:
                await self.agent.run_turn(prompt, author=author)
            except Exception:
                logger.exception("le tour a échoué dans %s", self.id)
            finally:
                # Envoyer libère : le jeton repart à la file. Sans ce `finally`,
                # un tour qui échoue laisserait le salon bloqué en `generating`.
                await self._apply(self.floor.end_turn())

        self._turn = asyncio.create_task(run())
        return Submission(started=True)

    # ------------------------------------------------------- jeton de parole

    async def request_floor(self, who: str, priority: int = 0) -> Outcome:
        return await self._apply(self.floor.request(who, priority))

    async def release_floor(self, who: str) -> Outcome:
        return await self._apply(self.floor.release(who))

    async def preempt_floor(self, who: str, priority: int = 0) -> Outcome:
        """Réquisitionne le jeton. L'appelant a vérifié `room.preempt`."""
        return await self._apply(self.floor.preempt(who, priority))

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

    async def _tick_forever(self) -> None:
        """Fait expirer les jetons abandonnés.

        Un porteur qui ferme son ordinateur portable sans se déconnecter
        proprement bloquerait sinon le salon jusqu'au redémarrage du serveur.
        """
        while True:
            await asyncio.sleep(TICK_INTERVAL_S)
            try:
                await self._apply(self.floor.tick())
            except Exception:
                logger.exception("échec du tic du jeton dans %s", self.id)

    # ------------------------------------------------------------ cycle de vie

    async def start(self) -> None:
        await self.agent.start()
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_forever())

    async def stop(self) -> bool:
        return await self.agent.interrupt()

    async def aclose(self) -> None:
        if self._chores:
            await asyncio.gather(*self._chores, return_exceptions=True)
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None
        # Avant l'interruption : une demande d'approbation en l'air empêcherait
        # le tour de se terminer, et donc le drainage d'aboutir.
        await self.approvals.abandon()
        if self._turn is not None and not self._turn.done():
            await self.agent.interrupt()
        await self.agent.stop()


class RoomManager:
    """Salons actifs du process.

    Un seul salon en v1, mais la forme est celle de l'étape 4 : les sessions sont
    épinglées à ce process, ce qui est exactement la limite que `Broadcaster`
    permettra de lever.
    """

    def __init__(self, broker: InProcessBroadcaster | None = None) -> None:
        self.broker = broker or InProcessBroadcaster()
        self._rooms: dict[str, Room] = {}

    def create(self, room_id: str, *, workspace: Path, **kwargs: Any) -> Room:
        if room_id in self._rooms:
            raise ValueError(f"le salon {room_id} existe déjà")
        room = Room(room_id, workspace=workspace, broker=self.broker, **kwargs)
        self._rooms[room_id] = room
        return room

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def list(self) -> list[Room]:
        return list(self._rooms.values())

    async def start_all(self) -> None:
        for room in self._rooms.values():
            await room.start()

    async def aclose(self) -> None:
        for room in self._rooms.values():
            await room.aclose()
        self._rooms.clear()
