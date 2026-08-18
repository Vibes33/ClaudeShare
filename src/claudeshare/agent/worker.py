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
    ) -> None:
        self.room_id = room_id
        self.workspace = workspace
        self._send = send
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
                await self.agent.run_turn(
                    str(data.get("prompt") or ""),
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

    async def aclose(self) -> None:
        # Les demandes en vol d'abord : une promesse non tenue empêcherait le
        # tour de se terminer, et donc le drainage d'aboutir.
        await self.approvals.abandon()
        if self._turn is not None and not self._turn.done():
            await self.agent.interrupt()
        await self.agent.stop()


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        #: Dossier de départ proposé à l'interface web. Le relais ne l'ouvre
        #: pas : il ne fait que le renvoyer pour pré-remplir un champ.
        self.base = base
        self.sandbox = sandbox
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
