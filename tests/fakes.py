"""Client Claude Code factice, pour tester le superviseur sans consommer l'abonnement.

Reproduit le contrat sur lequel le superviseur s'appuie :
`connect` / `query` / `receive_response` / `interrupt` / `disconnect`, et surtout
le fait que `receive_response()` se termine sur un `ResultMessage`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ResultMessage


def result(
    subtype: str = "success",
    *,
    session_id: str = "sess-1",
    terminal_reason: str | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id=session_id,
        terminal_reason=terminal_reason,
    )


@dataclass
class AskTool:
    """Marqueur de script : ici, le CLI demanderait la permission d'un outil.

    Rejouer le vrai chemin plutôt que d'appeler le courtier à la main : ça
    couvre le branchement `ClaudeAgentOptions.can_use_tool` du superviseur, qui
    est précisément l'endroit où l'oubli passerait inaperçu.
    """

    name: str = "Bash"
    input: dict[str, Any] = field(default_factory=lambda: {"command": "ls"})
    #: Décision obtenue, renseignée pendant la lecture du script.
    decision: Any = None


class FakeClient:
    """Rejoue des scripts de messages, un par tour."""

    def __init__(self, *, options: Any = None, scripts: list[list[Any]] | None = None) -> None:
        self.options = options
        self.scripts: list[list[Any]] = scripts or []
        self.prompts: list[str] = []
        self.interrupts = 0
        self.connected = False
        #: Quand il est posé, `receive_response` s'y bloque avant de produire le
        #: dernier message du script — de quoi simuler un tour long.
        self.gate: asyncio.Event | None = None
        #: Message injecté à la place de la fin de script après un interrupt.
        self.on_interrupt: Any = None
        #: Si vrai, aucun ResultMessage n'est produit après une interruption :
        #: c'est le cas que le chien de garde de drainage doit rattraper.
        self.never_finish = False

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self.gate is not None:
            self.gate.set()

    async def receive_response(self) -> AsyncIterator[Any]:
        script = self.scripts.pop(0) if self.scripts else [result()]
        for message in script[:-1]:
            if isinstance(message, AskTool):
                message.decision = await self.options.can_use_tool(
                    message.name, message.input, None
                )
                continue
            yield message
        if self.gate is not None:
            await self.gate.wait()
            self.gate = None
            if self.never_finish:
                await asyncio.sleep(3600)  # jamais de ResultMessage
            if self.on_interrupt is not None:
                yield self.on_interrupt
                return
        yield script[-1]


class LocalAgent:
    """Un agent branché sur un salon **sans socket**.

    Le vrai agent (`agent/worker.py`) parle au relais par WebSocket. Ici on
    court-circuite le seul transport, et on garde tout le reste : vrai
    `SessionSupervisor`, vrai courtier d'approbation, vraie `AgentLink`, vrai
    `dispatch` côté relais. Ce qui est exercé reste donc le contrat entre le
    salon et son agent, et pas une imitation qui divergerait au premier
    changement de protocole.

    Le transport lui-même est couvert séparément, par les tests qui montent une
    vraie socket d'agent.
    """

    def __init__(self, room, client, *, who: str = "hôte", workspace: str = "/tmp/agent") -> None:
        from claudeshare.agent import SessionSupervisor
        from claudeshare.agent.approval import ApprovalBroker
        from claudeshare.agent.toolpolicy import READ_ONLY_TOOLS, TrustLevel
        from claudeshare.protocol import AgentMessage
        from claudeshare.server.agentlink import AgentLink
        from claudeshare.server.ws_agents import dispatch

        self.room = room
        self.client = client
        self.who = who
        self._AgentMessage = AgentMessage
        self._dispatch = dispatch
        self._trust = TrustLevel.READER
        self._read_only = frozenset(READ_ONLY_TOOLS)
        self._TrustLevel = TrustLevel
        self.turns: list[asyncio.Task] = []

        self.link = AgentLink(room.id, who, send=self._order, sink=room.on_agent_event)
        self.approvals = ApprovalBroker(
            sink=self._event,
            context=lambda: (self.supervisor.current_author, self.supervisor.current_turn),
        )
        self.supervisor = SessionSupervisor(
            workspace=Path(workspace),
            sink=self._event,
            client_factory=self._bind,
            tools_gate=lambda: self._read_only if self._trust is TrustLevel.READER else None,
            can_use_tool=self.approvals.ask,
        )
        self.workspace = workspace

    def _bind(self, *, options):
        self.client.options = options
        return self.client

    async def _event(self, event) -> None:
        """Ce que fait `agent.event` : remonter l'événement tel quel au relais."""
        await self._dispatch(
            self._AgentMessage.AGENT_EVENT,
            {
                "type": str(event.type),
                "turn_id": event.turn_id,
                "author": event.author,
                "data": event.data,
            },
            self.link,
            self.room,
        )

    async def _order(self, message: dict) -> None:
        """Ce que le relais envoie à l'agent."""
        match message.get("type"):
            case self._AgentMessage.RUN_TURN:
                self._trust = self._TrustLevel(message.get("trust", "reader"))
                self.turns.append(asyncio.create_task(self._play(message)))
            case self._AgentMessage.RUN_INTERRUPT:
                await self.supervisor.interrupt()
            case self._AgentMessage.RUN_APPROVAL:
                await self.approvals.decide(
                    message["approval_id"],
                    allow=bool(message.get("allow")),
                    by=str(message.get("by") or "relais"),
                    reason=str(message.get("reason", "")),
                )

    async def _play(self, message: dict) -> None:
        turn_id = message["turn_id"]
        try:
            await self.supervisor.run_turn(
                message["prompt"], author=message["author"], turn_id=turn_id
            )
        finally:
            await self._dispatch(
                self._AgentMessage.AGENT_DONE,
                {"turn_id": turn_id, "session_id": self.supervisor.session_id},
                self.link,
                self.room,
            )

    async def attach(self) -> LocalAgent:
        await self.supervisor.start()
        self.room.host(self.link)
        await self._dispatch(
            self._AgentMessage.AGENT_HELLO,
            {"session_id": None, "workspace": self.workspace},
            self.link,
            self.room,
        )
        await self.room.announce_agent()
        return self

    async def detach(self) -> None:
        self.room.unhost(self.link)
        await self.supervisor.stop()
