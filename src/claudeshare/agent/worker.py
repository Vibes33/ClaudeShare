"""Le démon : la moitié qui exécute vraiment.

Tourne sur **votre** machine, avec **votre** abonnement et le CLI installé chez
vous. Se connecte en sortant vers le relais, une fois, et attend qu'on lui
confie des salons.

Ce renversement est ce qui débloque le projet. Avant l'étape 10, le serveur
était l'hôte : un seul abonnement, un seul compte capable de piloter, et rien ne
fonctionnait si cette personne n'était pas là. Avant l'étape 11, il fallait
relancer une commande par salon ; maintenant le démon reste ouvert et le relais
lui pousse les prises en charge décidées depuis l'interface web.

Trois choses restent ici et **ne doivent pas migrer vers le relais** :

1. **Les identifiants.** Ils ne quittent jamais cette machine. Le relais ne peut
   pas les perdre puisqu'il ne les a pas.
2. **Le shell.** Bac à sable, politique d'outils et hook `PreToolUse`
   s'appliquent ici — sur la machine qui a quelque chose à perdre si un invité
   obtient un outil de trop. Le relais annonce le niveau de confiance de
   l'auteur d'un tour ; c'est le démon qui en tire les conséquences, et il
   aurait tort de faire confiance sur ce point à un serveur qu'il ne contrôle
   pas.
3. **La promesse faite à `can_use_tool`.** Le délai et le refus par défaut vivent
   ici, parce que c'est ici que le SDK attend une réponse. Si le relais tombe en
   pleine demande, l'appel est refusé — jamais autorisé par inadvertance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from ..events import Event
from ..protocol import PROTOCOL_VERSION, AgentMessage
from .approval import ApprovalBroker
from .supervisor import SessionSupervisor
from .toolpolicy import READ_ONLY_TOOLS, TrustLevel

logger = logging.getLogger(__name__)

RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

#: Codes de fermeture qui ne valent pas la peine d'être retentés : le problème
#: n'est pas le réseau, il est du côté des droits.
FATAL_CLOSE_CODES = {4401: "jeton refusé — relancez `claudeshare login`"}


class Hosted:
    """Un salon pris en charge : sa session, son dossier, son courtier.

    Un objet par salon, et non un superviseur partagé : deux salons ont deux
    contextes, deux dossiers et deux interruptions, et les mêler ferait
    répondre l'un avec la mémoire de l'autre.
    """

    def __init__(
        self,
        room_id: str,
        workspace: Path,
        *,
        send: Any,
        sandbox: bool = True,
        session_id: str | None = None,
        client_factory: Any = None,
        confine: Path | None = None,
        fetch: Any = None,
    ) -> None:
        self.room_id = room_id
        self.workspace = workspace
        self._send = send
        #: Comment aller chercher une pièce jointe sur le relais. Injecté plutôt
        #: que câblé : c'est le démon qui détient l'adresse et le jeton, et les
        #: tests n'ont pas de relais à interroger.
        self._fetch = fetch
        #: Niveau de confiance du tour en cours, posé par le relais et appliqué
        #: par le hook à chaque appel d'outil.
        self._trust = TrustLevel.READER
        self._turn: asyncio.Task[Any] | None = None

        self.approvals = ApprovalBroker(
            sink=self._emit,
            context=lambda: (self.agent.current_author, self.agent.current_turn),
        )
        # La couture d'injection est le **client SDK**, pas le superviseur : le
        # remplacer en entier ferait perdre le branchement de `can_use_tool` et
        # du hook, c'est-à-dire précisément ce qu'on veut voir s'exercer.
        self.agent = SessionSupervisor(
            workspace=workspace,
            sink=self._emit,
            sandbox=sandbox,
            session_id=session_id,
            tools_gate=self._tools_gate,
            can_use_tool=self.approvals.ask,
            confine=confine,
            shared=True,
            **({"client_factory": client_factory} if client_factory else {}),
        )

    def _tools_gate(self) -> frozenset[str] | None:
        """Outils permis pour le tour en cours, selon la confiance de son auteur.

        Appliqué par le hook `PreToolUse`, pas par les options du SDK : celles-ci
        sont fixées à l'ouverture de la session, et un tour proposé par un
        lecteur doit être bridé sans rouvrir la session.
        """
        if self._trust is TrustLevel.READER:
            return frozenset(READ_ONLY_TOOLS)
        return None

    async def _emit(self, event: Event) -> None:
        """Remonte un événement du superviseur au relais, tel quel.

        Tel quel : le relais journalise et diffuse exactement ce que produisait
        le superviseur quand il tournait chez lui. C'est ce qui a permis de
        déplacer l'exécution sans toucher à la moitié aval du système.
        """
        await self._send(
            AgentMessage.AGENT_EVENT,
            room_id=self.room_id,
            type=str(event.type),
            turn_id=event.turn_id,
            author=event.author,
            data=event.data,
        )

    async def start(self) -> None:
        await self.agent.start()

    async def run(self, data: dict[str, Any]) -> None:
        """Joue un tour, en tâche de fond.

        En tâche de fond parce que le tour dure des minutes, et qu'il faut
        continuer à lire la socket pendant ce temps — c'est par elle qu'arrivent
        l'interruption et les décisions d'approbation, dont ce tour a précisément
        besoin pour se terminer.
        """
        turn_id = str(data.get("turn_id") or "")
        try:
            self._trust = TrustLevel(data.get("trust") or TrustLevel.READER)
        except ValueError:
            # Un niveau inconnu se résout au plus strict, jamais au plus large.
            self._trust = TrustLevel.READER

        async def jouer() -> None:
            try:
                prompt = await self._deposer(turn_id, data.get("attachments") or [])
                await self.agent.run_turn(
                    prompt + str(data.get("prompt") or ""),
                    author=str(data.get("author") or "?"),
                    turn_id=turn_id,
                )
            except Exception:
                logger.exception("le tour %s a échoué", turn_id)
            finally:
                # Toujours, même sur échec : sans cette trame le relais garderait
                # le jeton de parole pris par un tour qui n'existe plus. La
                # session voyage avec, parce qu'elle n'est connue qu'ici et
                # qu'elle est ce qui permettra la reprise (`resume`).
                await self._send(
                    AgentMessage.AGENT_DONE,
                    room_id=self.room_id,
                    turn_id=turn_id,
                    session_id=self.agent.session_id,
                )

        self._turn = asyncio.create_task(jouer())

    async def _deposer(self, turn_id: str, pieces: list[dict[str, Any]]) -> str:
        """Écrit les pièces jointes du tour, et renvoie le préambule du prompt.

        Elles atterrissent **dans le dossier de travail**, sous
        `.claudeshare/pieces-jointes/<tour>/`. C'est la seule place où la session
        puisse les lire : le bac à sable la confine à ce dossier, et un fichier
        déposé ailleurs serait invisible pour elle. Un sous-dossier par tour,
        parce que deux personnes qui joignent chacune `capture.png` dans le même
        salon ne doivent pas s'écraser l'une l'autre.

        Une pièce qui ne peut pas être récupérée n'annule pas le tour : la
        question posée vaut souvent d'être traitée sans, et un tour perdu pour
        un octet manquant serait un mauvais échange. Ce qui manque est **dit**
        dans le prompt, jamais passé sous silence.
        """
        if not pieces or self._fetch is None:
            return ""

        dossier = self.workspace / ".claudeshare" / "pieces-jointes" / turn_id
        lignes: list[str] = []
        for piece in pieces[:MAX_PIECES]:
            aid, nom = str(piece.get("id") or ""), str(piece.get("name") or "")
            # Revalidé ici, alors que le relais l'a déjà fait. C'est cette
            # machine-ci qui a quelque chose à perdre si un nom devient un
            # chemin : la défense tourne là où est le risque.
            if not IDENTIFIANT.match(aid) or not NOM_PIECE.match(nom) or ".." in nom:
                lignes.append(f"- (pièce jointe au nom refusé : {nom[:40]!r})")
                continue
            try:
                octets = await self._fetch(self.room_id, aid)
                dossier.mkdir(parents=True, exist_ok=True)
                (dossier / nom).write_bytes(octets)
            except Exception as exc:  # noqa: BLE001 — toute panne se dit, aucune n'annule
                logger.warning("pièce jointe %s indisponible : %s", aid, exc)
                lignes.append(f"- (pièce jointe « {nom} » non récupérée)")
                continue
            lignes.append(f"- .claudeshare/pieces-jointes/{turn_id}/{nom}")

        _balayer_pieces(self.workspace)
        entete = "\n".join(lignes)
        return (
            "Pièces jointes de ce message, déposées dans le dossier de travail :\n"
            f"{entete}\n\n"
        )

    async def aclose(self) -> None:
        # Les demandes en vol d'abord : une promesse non tenue empêcherait le
        # tour de se terminer, et donc le drainage d'aboutir.
        await self.approvals.abandon()
        if self._turn is not None and not self._turn.done():
            await self.agent.interrupt()
        await self.agent.stop()


def _texte(valeur: Any) -> str | None:
    """Une chaîne, ou `None` si le champ était absent.

    La distinction porte du sens jusqu'au superviseur : `""` veut dire « laisse
    le CLI choisir », `None` veut dire « ne touche pas à ce réglage ». Les
    confondre ferait remettre le modèle par défaut à chaque changement
    d'intensité.
    """
    return None if valeur is None else str(valeur)


#: Bornes reprises du relais. Redites ici parce que cette machine ne fait pas
#: confiance à ce qui lui arrive : c'est elle qui écrit sur son propre disque.
MAX_PIECES = 5
IDENTIFIANT = re.compile(r"^[0-9a-f]{16}$")
NOM_PIECE = re.compile(r"^[^\W_][\w .\-'(),+@]{0,79}$")

#: Au-delà, les dossiers de pièces jointes d'anciens tours sont retirés. Long,
#: parce qu'ils sont dans le dossier de quelqu'un : effacer vite ce qu'on a
#: déposé chez les gens est une surprise désagréable.
DUREE_PIECES_S = 7 * 24 * 3600

#: Miroir de la limite du relais. Un plafond de lecture, et non une confiance :
#: c'est ce qui empêche une réponse inattendue de remplir la mémoire ici.
MAX_OCTETS_PIECE = 10_000_000


def _balayer_pieces(workspace: Path) -> None:
    """Retire les dépôts d'anciens tours. Uniquement les nôtres.

    Restreint au dossier que nous créons et aux noms que nous fabriquons : rien
    de ce que quelqu'un aurait rangé là ne ressemble à un identifiant de tour,
    et c'est cette forme-là qui décide, pas l'emplacement seul.
    """
    racine = workspace / ".claudeshare" / "pieces-jointes"
    if not racine.is_dir():
        return
    limite = time.time() - DUREE_PIECES_S
    for dossier in racine.iterdir():
        try:
            if not dossier.is_dir() or not re.match(r"^[0-9a-f]{12}$", dossier.name):
                continue
            if dossier.stat().st_mtime >= limite:
                continue
            for fichier in dossier.iterdir():
                fichier.unlink(missing_ok=True)
            dossier.rmdir()
        except OSError:  # noqa: PERF203 — un dossier qui bouge sous nos pieds n'est pas une erreur
            continue


class Worker:
    """Le démon d'une personne : une socket, plusieurs salons."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        base: Path,
        sandbox: bool = True,
        connector: Any = None,
        client_factory: Any = None,
        confine: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        #: Dossier de départ proposé à l'interface web. Le relais ne l'ouvre
        #: pas : il ne fait que le renvoyer pour pré-remplir un champ.
        self.base = base
        self.sandbox = sandbox
        #: Racine hors de laquelle les accès fichiers sont refusés. Posée par le
        #: relais quand il lance l'agent lui-même, laissée vide sur une machine
        #: personnelle où l'on est déjà chez soi.
        self.confine = confine
        self.status = "hors ligne"
        self.fatal: str | None = None
        self.hosted: dict[str, Hosted] = {}

        self._token = token
        self._connector = connector
        #: Couture d'injection : les tests fournissent un client SDK factice
        #: plutôt que de démarrer un vrai CLI par salon.
        self._client_factory = client_factory
        self._socket: Any = None
        self._backoff = RECONNECT_MIN_S

    async def _piece_jointe(self, room_id: str, aid: str) -> bytes:
        """Récupère une pièce jointe sur le relais.

        `urllib` dans un fil plutôt qu'un client HTTP asynchrone : l'agent n'a
        pas d'autre appel HTTP à passer, et tirer une dépendance de plus pour
        une requête par pièce jointe serait cher payé. Le fil évite de bloquer
        la boucle pendant le transfert.
        """
        url = f"{self.base_url}/api/rooms/{room_id}/attachments/{aid}"
        requete = urllib.request.Request(  # noqa: S310 — schéma fixé par `base_url`
            url, headers={"Authorization": f"Bearer {self._token}"}
        )

        def lire() -> bytes:
            with urllib.request.urlopen(requete, timeout=60) as reponse:  # noqa: S310
                return reponse.read(MAX_OCTETS_PIECE + 1)

        octets = await asyncio.to_thread(lire)
        if len(octets) > MAX_OCTETS_PIECE:
            raise ValueError("pièce jointe plus lourde qu'annoncé")
        return octets

    @property
    def url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        hote = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{hote}/ws/agent"

    # ------------------------------------------------------------- émission

    async def _send(self, type_: str, **data: Any) -> None:
        if self._socket is None:
            return
        with contextlib.suppress(Exception):
            await self._socket.send(
                json.dumps({"v": PROTOCOL_VERSION, "type": str(type_), "data": data})
            )

    # ------------------------------------------------------------ réception

    async def _handle(self, kind: str, data: dict[str, Any]) -> None:
        room_id = str(data.get("room_id") or "")
        match kind:
            case AgentMessage.RUN_HOST:
                await self._host(room_id, data)
            case AgentMessage.RUN_UNHOST:
                await self._unhost(room_id)
            case AgentMessage.RUN_TURN:
                if salon := self.hosted.get(room_id):
                    await salon.run(data)
            case AgentMessage.RUN_CONFIGURE:
                if salon := self.hosted.get(room_id):
                    await salon.agent.configure(
                        model=_texte(data.get("model")),
                        effort=_texte(data.get("effort")),
                    )
            case AgentMessage.RUN_INTERRUPT:
                if salon := self.hosted.get(room_id):
                    await salon.agent.interrupt()
            case AgentMessage.RUN_APPROVAL:
                if salon := self.hosted.get(room_id):
                    await salon.approvals.decide(
                        str(data.get("approval_id", "")),
                        allow=bool(data.get("allow")),
                        by=str(data.get("by") or "relais"),
                        reason=str(data.get("reason", "")),
                    )
            case _:
                logger.debug("ordre inconnu : %s", kind)

    async def _host(self, room_id: str, data: dict[str, Any]) -> None:
        """Prend en charge un salon dans le dossier demandé.

        Le dossier vient du relais, donc d'un formulaire web. Il désigne un
        chemin **sur cette machine** : on le résout et on refuse ce qui n'existe
        pas, plutôt que de laisser le SDK échouer plus tard sur un message qui
        ne dira pas d'où vient le problème.
        """
        if room_id in self.hosted:
            await self._confirm(room_id)
            return

        chemin = Path(str(data.get("workspace") or self.base)).expanduser()
        try:
            chemin = chemin.resolve(strict=True)
            if not chemin.is_dir():
                raise NotADirectoryError(chemin)
        except (OSError, NotADirectoryError) as exc:
            logger.error("dossier refusé pour %s : %s", room_id, exc)
            await self._send(
                AgentMessage.AGENT_HOSTED,
                room_id=room_id,
                ok=False,
                error=f"dossier introuvable sur cette machine : {chemin}",
            )
            return

        salon = Hosted(
            room_id,
            chemin,
            send=self._send,
            sandbox=self.sandbox,
            session_id=str(data.get("session_id") or "") or None,
            client_factory=self._client_factory,
            confine=self.confine,
            fetch=self._piece_jointe,
        )
        try:
            await salon.start()
        except Exception as exc:  # noqa: BLE001 — remonté à l'interface, pas avalé
            logger.exception("session refusée pour %s", room_id)
            await self._send(
                AgentMessage.AGENT_HOSTED, room_id=room_id, ok=False, error=str(exc)
            )
            return

        self.hosted[room_id] = salon
        logger.info("salon %s pris en charge dans %s", room_id, chemin)
        await self._confirm(room_id)

    async def _confirm(self, room_id: str) -> None:
        salon = self.hosted[room_id]
        await self._send(
            AgentMessage.AGENT_HOSTED,
            room_id=room_id,
            ok=True,
            workspace=str(salon.workspace),
            session_id=salon.agent.session_id,
        )

    async def _unhost(self, room_id: str) -> None:
        salon = self.hosted.pop(room_id, None)
        if salon is None:
            return
        await salon.aclose()
        # Annoncé au relais, et pas seulement fait ici : sans cette trame il
        # continuerait de se croire hébergé, et les prompts partiraient vers une
        # session qui n'existe plus — un salon qui paraît sain et ne répond pas.
        await self._send(AgentMessage.AGENT_HOSTED, room_id=room_id, ok=False, error="")
        logger.info("salon %s lâché", room_id)

    # ------------------------------------------------------------ connexion

    def _open(self):
        if self._connector is not None:
            return self._connector(self.url, self._token)

        import websockets

        return websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self._token}"}
        )

    async def _session(self) -> None:
        async with self._open() as socket:
            self._socket = socket
            self._backoff = RECONNECT_MIN_S
            self.status = "connecté"
            await self._send(
                AgentMessage.AGENT_HELLO,
                base=str(self.base),
                platform=platform.system(),
            )
            # Après une coupure, le relais a oublié nos prises en charge : on les
            # réannonce plutôt que d'attendre que quelqu'un reclique.
            for room_id in list(self.hosted):
                await self._confirm(room_id)
            logger.info("démon connecté à %s", self.base_url)

            async for brut in socket:
                try:
                    trame = json.loads(brut)
                except (TypeError, ValueError):
                    continue
                if isinstance(trame, dict):
                    await self._handle(str(trame.get("type", "")), trame.get("data") or trame)

    async def run(self) -> None:
        """Sert le relais jusqu'à l'arrêt."""
        try:
            while self.fatal is None:
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — toute panne réseau se retente
                    self._note(exc)
                finally:
                    self._socket = None
                    self.status = "hors ligne"

                if self.fatal is not None:
                    break
                logger.info("reconnexion dans %.0f s", self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, RECONNECT_MAX_S)
        finally:
            await self.aclose()

    def _note(self, exc: Exception) -> None:
        code = getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)
        if code in FATAL_CLOSE_CODES:
            self.fatal = FATAL_CLOSE_CODES[code]
        else:
            logger.debug("connexion perdue : %s", exc)

    async def aclose(self) -> None:
        for room_id in list(self.hosted):
            await self._unhost(room_id)
