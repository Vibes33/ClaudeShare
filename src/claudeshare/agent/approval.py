"""Approbation d'outil par un humain distant.

`can_use_tool` est une **coroutine** : elle peut donc attendre la décision de
quelqu'un d'autre. C'est le raccord naturel du projet — l'agent demande, le
salon tranche.

Trois règles, dans cet ordre d'importance :

1. **Un délai se résout en refus, jamais l'inverse.** Personne pour répondre ne
   doit jamais valoir accord tacite.
2. **La première réponse tranche.** Le contraire — attendre un quorum — bloque
   sur la première absence.
3. **Une annulation refuse aussi.** Quand un tour est interrompu pendant qu'une
   demande est en l'air, la coroutine est annulée : sans le `finally`, la
   promesse resterait en attente et le tour ne se terminerait jamais.

⚠ **Ce garde-fou ne voit que ce qui l'atteint.** Un outil auto-approuvé
(`allowed_tools`) n'appelle jamais `can_use_tool` — c'est documenté dans
`toolpolicy.py` et c'est la raison d'être du hook `PreToolUse`, qui, lui,
s'exécute toujours. Les deux couches sont complémentaires : celle-ci demande à
un humain, l'autre trace et interdit sans demander.

Volontairement laissé de côté en v1 : `PermissionResultAllow(updated_input=…)`,
qui permettrait de corriger l'entrée d'un outil avant de l'autoriser. C'est
séduisant, mais accepter un dictionnaire arbitraire venu d'un client demande sa
propre validation, et un « oui mais avec ça à la place » mal formé se lit très
mal côté Claude. Approuver ou refuser suffit pour l'instant.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from ..events import Event, EventType

logger = logging.getLogger(__name__)

#: Au-delà, l'appel est refusé. Assez long pour que quelqu'un voie l'invite et
#: réponde, assez court pour qu'un salon désert ne fige pas un tour.
APPROVAL_TIMEOUT_S = 120.0

#: Message renvoyé à Claude sur expiration. Il lui est adressé, pas à l'humain :
#: c'est ce qu'il lira pour décider de la suite.
TIMEOUT_MESSAGE = (
    "Aucun participant n'a approuvé cet appel dans le délai imparti. "
    "L'appel est refusé ; propose une autre approche ou demande des précisions."
)
CANCELLED_MESSAGE = "Le tour a été interrompu pendant la demande d'approbation."


@dataclass(slots=True)
class Pending:
    """Une demande en attente de réponse."""

    id: str
    tool: str
    input: dict[str, Any]
    #: Participant à l'origine du tour. Sert à empêcher l'auto-approbation.
    author: str | None
    turn_id: str | None
    asked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    future: asyncio.Future[tuple[bool, str, str]] = field(repr=False, default=None)  # type: ignore[assignment]

    def view(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "tool": self.tool,
            "input": self.input,
            "author": self.author,
            "turn_id": self.turn_id,
            "asked_at": self.asked_at.isoformat(),
        }


class ApprovalBroker:
    """Fait le pont entre `can_use_tool` et une décision humaine.

    Ne connaît **pas** les droits : savoir qui a le droit de trancher est le
    travail de `server/ws.py`, qui résout les capacités. Ici on ne gère que
    l'attente, le délai et l'unicité de la réponse.
    """

    def __init__(
        self,
        *,
        sink: Callable[[Event], Awaitable[None]],
        timeout: float = APPROVAL_TIMEOUT_S,
        context: Callable[[], tuple[str | None, str | None]] | None = None,
    ) -> None:
        self._sink = sink
        self._timeout = timeout
        #: Renvoie (auteur, turn_id) du tour en cours — fourni par le
        #: superviseur, qui est le seul à le savoir au moment de l'appel.
        self._context = context or (lambda: (None, None))
        self._pending: dict[str, Pending] = {}

    # ------------------------------------------------------------- lecture

    def pending(self) -> list[dict[str, Any]]:
        """Demandes en cours, pour l'instantané d'un client qui (re)connecte.

        Sans ça, quelqu'un qui arrive pendant une demande verrait un tour figé
        sans savoir pourquoi.
        """
        return [p.view() for p in self._pending.values()]

    def get(self, approval_id: str) -> Pending | None:
        return self._pending.get(approval_id)

    # ---------------------------------------------------------- can_use_tool

    async def ask(
        self, tool_name: str, tool_input: dict[str, Any], _context: Any = None
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Point d'entrée branché sur `ClaudeAgentOptions.can_use_tool`."""
        author, turn_id = self._context()
        demande = Pending(
            id=uuid.uuid4().hex[:12],
            tool=tool_name,
            input=tool_input if isinstance(tool_input, dict) else {},
            author=author,
            turn_id=turn_id,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[demande.id] = demande

        await self._emit(EventType.TOOL_APPROVAL_REQUESTED, demande, demande.view())

        try:
            accorde, par, motif = await asyncio.wait_for(demande.future, self._timeout)
        except TimeoutError:
            return await self._settle(demande, False, "", TIMEOUT_MESSAGE, "timeout")
        except asyncio.CancelledError:
            # Tour interrompu pendant l'attente. On résout quand même, sinon la
            # demande resterait affichée pour toujours côté clients.
            await self._settle(demande, False, "", CANCELLED_MESSAGE, "cancelled")
            raise
        return await self._settle(demande, accorde, par, motif, "decided")

    async def decide(
        self, approval_id: str, *, allow: bool, by: str, reason: str = ""
    ) -> bool:
        """Enregistre une décision. False si la demande n'existe plus.

        Le cas « plus là » est ordinaire, pas une erreur : deux personnes
        peuvent cliquer en même temps, et la seconde arrive après coup.
        """
        demande = self._pending.get(approval_id)
        if demande is None or demande.future.done():
            return False
        demande.future.set_result((allow, by, reason))
        return True

    async def abandon(self, motif: str = CANCELLED_MESSAGE) -> None:
        """Refuse tout ce qui est en attente. Appelé à la fermeture du salon."""
        for demande in list(self._pending.values()):
            if not demande.future.done():
                demande.future.set_result((False, "", motif))

    # ------------------------------------------------------------ internes

    async def _settle(
        self, demande: Pending, allow: bool, by: str, reason: str, how: str
    ) -> PermissionResultAllow | PermissionResultDeny:
        self._pending.pop(demande.id, None)
        await self._emit(
            EventType.TOOL_APPROVAL_RESOLVED,
            demande,
            {
                "approval_id": demande.id,
                "tool": demande.tool,
                "allowed": allow,
                "by": by or None,
                "how": how,
                "reason": reason,
            },
        )
        if allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason or "Appel refusé par un participant.")

    async def _emit(self, type_: EventType, demande: Pending, data: dict[str, Any]) -> None:
        await self._sink(
            Event(type=type_, turn_id=demande.turn_id, author=demande.author, data=data)
        )
