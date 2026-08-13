"""Client Claude Code factice, pour tester le superviseur sans consommer l'abonnement.

Reproduit le contrat sur lequel le superviseur s'appuie :
`connect` / `query` / `receive_response` / `interrupt` / `disconnect`, et surtout
le fait que `receive_response()` se termine sur un `ResultMessage`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
