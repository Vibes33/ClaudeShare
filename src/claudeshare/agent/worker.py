"""L'agent : la moitié qui exécute vraiment.

Tourne sur **votre** machine, avec **votre** abonnement, **votre** dossier de
travail et le CLI installé chez vous. Se connecte en sortant vers le relais et
attend qu'on lui donne des tours à jouer.

Ce renversement est ce qui débloque le projet. Avant, le serveur était l'hôte :
un seul abonnement, un seul compte capable de piloter, et rien ne fonctionnait
si cette personne n'était pas là. Maintenant chacun héberge ses propres salons,
et rejoint ceux des autres pour proposer.

Trois choses restent ici et **ne doivent pas migrer vers le relais** :

1. **Les identifiants.** Ils ne quittent jamais cette machine. Le relais ne peut
   pas les perdre puisqu'il ne les a pas.
2. **Le shell.** Bac à sable, politique d'outils et hook `PreToolUse`
   s'appliquent ici — sur la machine qui a quelque chose à perdre si un invité
   obtient un outil de trop. Le relais annonce le niveau de confiance de
   l'auteur d'un tour ; c'est l'agent qui en tire les conséquences, et il aurait
   tort de faire confiance sur ce point à un serveur qu'il ne contrôle pas.
3. **La promesse faite à `can_use_tool`.** Le délai et le refus par défaut vivent
   ici, parce que c'est ici que le SDK attend une réponse. Si le relais tombe en
   pleine demande, l'appel est refusé — jamais autorisé par inadvertance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
FATAL_CLOSE_CODES = {4401: "jeton refusé — relancez `claudeshare login`",
                     4403: "vous n'avez pas le droit d'héberger ce salon",
                     4404: "salon inconnu"}


class Worker:
    """Un agent attaché à un salon.

    Un salon = un agent = une session Claude Code. Héberger deux salons demande
    deux agents : leurs contextes, leurs dossiers et leurs interruptions n'ont
    aucune raison d'être mêlés.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        room_id: str,
        *,
        workspace: Path,
        sandbox: bool = True,
        session_id: str | None = None,
        supervisor: SessionSupervisor | None = None,
        connector: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.room_id = room_id
        self.workspace = workspace
        self.status = "hors ligne"
        self.fatal: str | None = None

        self._token = token
        self._connector = connector
        self._socket: Any = None
        self._backoff = RECONNECT_MIN_S
        #: Niveau de confiance du tour en cours, posé par le relais et appliqué
        #: par le hook à chaque appel d'outil.
        self._trust = TrustLevel.READER
        self._turn: asyncio.Task[Any] | None = None

        self.approvals = ApprovalBroker(
            sink=self._emit,
            context=lambda: (self.agent.current_author, self.agent.current_turn),
        )
        self.agent = supervisor or SessionSupervisor(
            workspace=workspace,
            sink=self._emit,
            sandbox=sandbox,
            session_id=session_id,
            tools_gate=self._tools_gate,
            can_use_tool=self.approvals.ask,
            shared=True,
        )

    @property
    def url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        hote = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{hote}/ws/agents/{self.room_id}"

    def _tools_gate(self) -> frozenset[str] | None:
        """Outils permis pour le tour en cours, selon la confiance de son auteur.

        Appliqué par le hook `PreToolUse`, pas par les options du SDK : celles-ci
        sont fixées à l'ouverture de la session, et un tour proposé par un
        lecteur doit être bridé sans rouvrir la session.
        """
        if self._trust is TrustLevel.READER:
            return frozenset(READ_ONLY_TOOLS)
        return None

    # ------------------------------------------------------------- émission

    async def _send(self, type_: str, **data: Any) -> None:
        if self._socket is None:
            return
        with contextlib.suppress(Exception):
            await self._socket.send(
                json.dumps({"v": PROTOCOL_VERSION, "type": str(type_), "data": data})
            )

    async def _emit(self, event: Event) -> None:
        """Remonte un événement du superviseur au relais, tel quel.

        Tel quel : le relais journalise et diffuse exactement ce que produisait
        le superviseur quand il tournait chez lui. C'est ce qui a permis de
        déplacer l'exécution sans toucher à la moitié aval du système.
        """
        await self._send(
            AgentMessage.AGENT_EVENT,
            type=str(event.type),
            turn_id=event.turn_id,
            author=event.author,
            data=event.data,
        )

    # ------------------------------------------------------------ réception

    async def _handle(self, kind: str, data: dict[str, Any]) -> None:
        match kind:
            case AgentMessage.RUN_TURN:
                await self._run(data)
            case AgentMessage.RUN_INTERRUPT:
                await self.agent.interrupt()
            case AgentMessage.RUN_APPROVAL:
                await self.approvals.decide(
                    str(data.get("approval_id", "")),
                    allow=bool(data.get("allow")),
                    by=str(data.get("by") or "relais"),
                    reason=str(data.get("reason", "")),
                )
            case _:
                logger.debug("ordre inconnu : %s", kind)

    async def _run(self, data: dict[str, Any]) -> None:
        """Joue un tour, puis rend la main au relais.

        En tâche de fond : le tour dure des minutes, et pendant ce temps il faut
        continuer à lire la socket — c'est par elle qu'arrivent l'interruption
        et les décisions d'approbation, dont ce tour a précisément besoin pour
        se terminer.
        """
        turn_id = str(data.get("turn_id") or "")
        prompt = str(data.get("prompt") or "")
        author = str(data.get("author") or "?")
        try:
            self._trust = TrustLevel(data.get("trust") or TrustLevel.READER)
        except ValueError:
            # Un niveau inconnu se résout au plus strict, jamais au plus large.
            self._trust = TrustLevel.READER

        async def jouer() -> None:
            try:
                await self.agent.run_turn(prompt, author=author, turn_id=turn_id)
            except Exception:
                logger.exception("le tour %s a échoué", turn_id)
            finally:
                # Toujours, même sur échec : sans cette trame le relais garderait
                # le jeton de parole pris par un tour qui n'existe plus. La
                # session voyage avec, parce qu'elle n'est connue qu'ici et
                # qu'elle est ce qui permettra la reprise (`resume`).
                await self._send(
                    AgentMessage.AGENT_DONE,
                    turn_id=turn_id,
                    session_id=self.agent.session_id,
                )

        self._turn = asyncio.create_task(jouer())

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
                session_id=self.agent.session_id,
                workspace=str(self.workspace),
            )
            logger.info("agent attaché à %s", self.room_id)

            async for brut in socket:
                try:
                    trame = json.loads(brut)
                except (TypeError, ValueError):
                    continue
                if isinstance(trame, dict):
                    await self._handle(str(trame.get("type", "")), trame.get("data") or trame)

    async def run(self) -> None:
        """Démarre la session Claude et sert le relais jusqu'à l'arrêt."""
        await self.agent.start()
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
        # Les demandes en vol d'abord : une promesse non tenue empêcherait le
        # tour de se terminer, et donc le drainage d'aboutir.
        await self.approvals.abandon()
        if self._turn is not None and not self._turn.done():
            await self.agent.interrupt()
        await self.agent.stop()
