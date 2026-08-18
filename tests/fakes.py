"""Client Claude Code factice, pour tester le superviseur sans consommer l'abonnement.

Reproduit le contrat sur lequel le superviseur s'appuie :
`connect` / `query` / `receive_response` / `interrupt` / `disconnect`, et surtout
le fait que `receive_response()` se termine sur un `ResultMessage`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
    """Un démon branché sur un salon **sans socket réseau**.

    Bâti sur le vrai `Worker` et la vraie `AgentSession` : seul le transport est
    court-circuité, par une paire de files qui tient lieu de socket. Superviseur,
    courtier d'approbation, liaison, dispatch — tout le reste est le code de
    production. Ce qui est exercé reste donc le contrat entre le salon et son
    démon, et pas une imitation qui divergerait au premier changement de
    protocole.
    """

    def __init__(self, room, client, *, who: str = "hôte", workspace: str | None = None) -> None:
        import tempfile

        from claudeshare.agent.worker import Worker
        from claudeshare.protocol import PROTOCOL_VERSION
        from claudeshare.server.daemons import AgentDaemon
        from claudeshare.server.ws_agents import AgentSession, parse_agent_message

        self.room = room
        self.client = client
        self.who = who
        self.workspace = workspace or tempfile.mkdtemp(prefix="cs-agent-")
        self._version = PROTOCOL_VERSION
        self._parse = parse_agent_message

        #: Ordres du relais vers le démon. Le démon les lit comme une socket.
        self._vers_demon: asyncio.Queue[str] = asyncio.Queue()

        self.daemon = AgentDaemon(f"usr-{who}", who, send=self._au_demon)
        self.session = AgentSession(
            self.daemon,
            room_of=self._salon,
            may_host=lambda _room_id: True,
        )
        self.worker = Worker(
            "http://relais",
            "jeton",
            base=Path(self.workspace),
            connector=self._transport,
            client_factory=self._bind,
        )
        self._boucle: asyncio.Task | None = None

    # -------------------------------------------------------- le transport

    def _transport(self, url: str, token: str):
        @contextlib.asynccontextmanager
        async def ouvrir():
            yield self._Socket(self)

        return ouvrir()

    class _Socket:
        """Ce que le démon prend pour une socket."""

        def __init__(self, agent: LocalAgent) -> None:
            self._agent = agent

        async def send(self, brut: str) -> None:
            kind, data = self._agent._parse(json.loads(brut))
            await self._agent.session.handle(kind, data)

        async def __aiter__(self):
            while True:
                yield await self._agent._vers_demon.get()

    async def _au_demon(self, message: dict) -> None:
        type_ = message.pop("type")
        await self._vers_demon.put(
            json.dumps({"v": self._version, "type": type_, "data": message})
        )

    async def _salon(self, room_id: str):
        return self.room if room_id == self.room.id else None

    def _bind(self, *, options):
        """Le client SDK factice, à la place du vrai CLI."""
        self.client.options = options
        return self.client

    # ------------------------------------------------------------ cycle

    async def attach(self) -> LocalAgent:
        """Démarre le démon et lui fait prendre le salon en charge."""
        self._boucle = asyncio.create_task(self.worker.run())
        await self._attendre(lambda: self.worker.status == "connecté")
        await self.daemon.host(self.room.id, self.workspace)
        await self._attendre(lambda: self.room.hosted)
        return self

    async def detach(self) -> None:
        await self.session.release(self.room.id)
        await self.worker._unhost(self.room.id)

    async def aclose(self) -> None:
        await self.detach()
        if self._boucle is not None:
            self._boucle.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._boucle

    @staticmethod
    async def _attendre(condition, limite: int = 200) -> None:
        for _ in range(limite):
            if condition():
                return
            await asyncio.sleep(0)
        raise AssertionError("le démon n'a jamais atteint l'état attendu")
