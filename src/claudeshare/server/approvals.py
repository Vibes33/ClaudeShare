"""Ce que le relais sait des approbations d'outil en cours.

À ne pas confondre avec `agent/approval.py`, qui est le vrai courtier : c'est
lui qui tient la promesse rendue à `can_use_tool`, applique le délai et décide
du refus. Il vit chez l'agent, parce que c'est là que l'appel d'outil se produit
et que quelqu'un attend une réponse.

Le relais, lui, n'a qu'un travail : **savoir ce qui attend, pour l'afficher et
pour router la décision**. Il apprend l'existence d'une demande en voyant passer
l'événement `tool.approval_requested` que l'agent diffuse de toute façon, et la
voit disparaître au `tool.approval_resolved`. Aucun message dédié : deux chemins
pour un même fait finissent toujours par diverger.

Conséquence à assumer : si le relais tombe, les demandes en vol ne sont pas
perdues pour autant — c'est le délai de l'agent qui tranche, et il tranche en
refus. Le pire cas est donc un appel refusé, jamais un appel autorisé par
inadvertance.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..events import Event, EventType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Waiting:
    """Une demande en attente, telle que le relais la connaît."""

    approval_id: str
    tool: str
    input: dict[str, Any]
    #: Auteur du tour. Sert au garde-fou d'auto-approbation, dans `ws.py`.
    author: str | None
    turn_id: str | None
    asked_at: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "input": self.input,
            "author": self.author,
            "turn_id": self.turn_id,
            "asked_at": self.asked_at,
        }


class ApprovalDesk:
    """Le guichet du relais : qui attend, et à qui renvoyer la réponse."""

    def __init__(self, answer: Callable[..., Awaitable[None]]) -> None:
        #: Transmet la décision à l'agent. Fourni par le salon, qui sait quelle
        #: liaison est active à cet instant.
        self._answer = answer
        self._waiting: dict[str, Waiting] = {}

    # ------------------------------------------------------------- lecture

    def pending(self) -> list[dict[str, Any]]:
        """Demandes en cours, pour l'instantané d'un client qui (re)connecte.

        Sans ça, quelqu'un qui arrive pendant une demande verrait un tour figé
        sans savoir pourquoi.
        """
        return [w.view() for w in self._waiting.values()]

    def get(self, approval_id: str) -> Waiting | None:
        return self._waiting.get(approval_id)

    # ------------------------------------------------------------ écoute

    def observe(self, event: Event) -> None:
        """Suit le flux d'événements de l'agent pour tenir la liste à jour."""
        data = event.data or {}
        approval_id = data.get("approval_id")
        if not approval_id:
            return

        if event.type is EventType.TOOL_APPROVAL_REQUESTED:
            self._waiting[approval_id] = Waiting(
                approval_id=approval_id,
                tool=str(data.get("tool", "")),
                input=data.get("input") or {},
                author=event.author,
                turn_id=event.turn_id,
                asked_at=str(data.get("asked_at", "")),
            )
        elif event.type is EventType.TOOL_APPROVAL_RESOLVED:
            self._waiting.pop(approval_id, None)

    # ------------------------------------------------------------ décision

    async def decide(
        self, approval_id: str, *, allow: bool, by: str, reason: str = ""
    ) -> bool:
        """Transmet une décision. False si la demande n'existe plus.

        Le cas « plus là » est ordinaire, pas une erreur : deux personnes
        peuvent cliquer en même temps, et la seconde arrive après coup. On
        retire l'entrée tout de suite pour que la seconde le voie, sans attendre
        que l'agent confirme.
        """
        if self._waiting.pop(approval_id, None) is None:
            return False
        logger.info("approbation %s : %s par %s", approval_id, "oui" if allow else "non", by)
        await self._answer(approval_id, allow=allow, by=by, reason=reason)
        return True

    def forget(self) -> None:
        """Oublie tout. Appelé quand l'agent se déconnecte.

        On ne refuse pas explicitement : c'est le délai de l'agent qui s'en
        charge, et il est le seul à pouvoir répondre à la promesse en vol.
        Prétendre ici que tout est refusé annoncerait une décision qui n'a pas
        été prise.
        """
        self._waiting.clear()
