"""Vocabulaire d'événements partagé par le superviseur, le journal et le protocole WS.

Séparé du SDK à dessein : le superviseur traduit les messages du SDK en ces
événements, et tout le reste du programme ne connaît qu'eux. Ça garde une seule
frontière à ajuster quand le SDK évolue.

Distinction structurante — certains événements sont **durables** (ils décrivent ce
qui s'est passé et sont rejoués à la reconnexion), d'autres sont **éphémères**
(deltas de streaming, diffusés en direct et jamais écrits). `Event.durable` porte
cette distinction ; le journal s'en sert pour décider ce qu'il persiste.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    # --- cycle de vie de la session ---
    SESSION_READY = "session.ready"
    SESSION_ERROR = "session.error"

    # --- déroulement d'un tour ---
    TURN_STARTED = "turn.started"
    TURN_ENDED = "turn.ended"

    # --- production de l'assistant ---
    ASSISTANT_DELTA = "assistant.delta"  # éphémère
    ASSISTANT_MESSAGE = "assistant.message"
    THINKING_STARTED = "thinking.started"  # éphémère, signal de progression

    # --- outils ---
    TOOL_USE = "tool.use"
    TOOL_RESULT = "tool.result"

    # --- quotas d'abonnement ---
    RATE_LIMIT = "rate_limit"


#: Événements diffusés en direct mais jamais persistés. Un client qui se
#: reconnecte reçoit le journal, puis le partiel en cours gardé en mémoire.
EPHEMERAL: frozenset[str] = frozenset(
    {EventType.ASSISTANT_DELTA, EventType.THINKING_STARTED}
)


@dataclass(slots=True)
class Event:
    """Un fait observable dans un salon."""

    type: EventType
    #: Tour auquel l'événement se rattache. None pour les événements de session.
    turn_id: str | None = None
    #: Participant à l'origine du tour. None quand c'est le système.
    author: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def durable(self) -> bool:
        return self.type not in EPHEMERAL

    def payload(self) -> dict[str, Any]:
        """Corps aplati, tel qu'il voyage sur le fil.

        `data` est remonté d'un niveau : sans ça les clients écriraient
        `frame.data.data.text`, une imbriquation qui n'apporte rien et qu'on
        oublie une fois sur deux.
        """
        return {"turn_id": self.turn_id, "author": self.author, **self.data}

    def to_dict(self) -> dict[str, Any]:
        return {"type": str(self.type), **self.payload()}


EventSink = Any  # Callable[[Event], Awaitable[None]] — allégé pour éviter un import cyclique
