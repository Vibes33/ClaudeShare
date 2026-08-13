"""Un salon : une session Claude Code, un journal, des abonnés.

Le salon est le point où les trois briques se rejoignent — le superviseur
produit des événements, le journal les numérote, le diffuseur les distribue.
C'est aussi le seul endroit qui décide : les clients envoient des intentions,
jamais des ordres.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..agent import SessionSupervisor, TurnBusyError
from ..agent.hooks import AuditRecord
from ..agent.toolpolicy import READ_ONLY_TOOLS, TrustLevel
from ..core.broker import InProcessBroadcaster
from ..core.eventlog import EventLog
from ..events import Event
from ..protocol import ServerMessage, envelope

logger = logging.getLogger(__name__)




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
    ) -> None:
        self.id = room_id
        self.title = title or room_id
        self.workspace = workspace
        self.log = EventLog()
        self.broker = broker
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

    async def left(self, who: str) -> None:
        remaining = self._present.get(who, 1) - 1
        if remaining > 0:
            self._present[who] = remaining
            return
        self._present.pop(who, None)
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
            },
            seq=self.log.last_seq,
        )

    async def submit(
        self, prompt: str, author: str, trust: TrustLevel = TrustLevel.WRITER
    ) -> None:
        """Lance un tour en tâche de fond.

        On ne bloque pas l'appelant : le tour dure des minutes, et sa progression
        arrive à tout le monde par la diffusion, pas par cette réponse. La mise
        en file relèvera du jeton de parole (étape 7) ; ici deux envois simultanés
        se soldent par un refus.
        """
        if self.agent.busy:
            raise TurnBusyError("un tour est déjà en cours dans ce salon")

        self._turn_trust = trust

        async def run() -> None:
            try:
                await self.agent.run_turn(prompt, author=author)
            except Exception:
                logger.exception("le tour a échoué dans %s", self.id)

        self._turn = asyncio.create_task(run())

    async def stop(self) -> bool:
        return await self.agent.interrupt()

    async def aclose(self) -> None:
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
            await room.agent.start()

    async def aclose(self) -> None:
        for room in self._rooms.values():
            await room.aclose()
        self._rooms.clear()
