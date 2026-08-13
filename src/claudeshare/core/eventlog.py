"""Journal de collaboration : qui a fait quoi, dans quel ordre.

À ne pas confondre avec la session Claude Code, qui possède le *contexte du
modèle* et que le SDK persiste de son côté. Ce journal-ci enregistre la
collaboration — prompts, appels d'outils, prises de parole — et c'est lui qu'on
rejoue pour l'affichage et la reconnexion.

Deux mécanismes complémentaires, et la distinction est structurante :

- Les événements **durables** reçoivent un `seq` monotone et sont conservés. Les
  clients dédoublonnent dessus à la reconnexion.
- Les **deltas de streaming** ne sont jamais journalisés. Ils sont diffusés en
  direct et accumulés dans un tampon volatile, pour qu'un client qui arrive au
  milieu d'un tour reçoive le texte déjà produit sans qu'on ait écrit des
  milliers de lignes.

En v1 tout est en mémoire : un redémarrage du serveur perd l'historique de
collaboration (pas le contexte de Claude, que `resume` retrouve). La persistance
viendra avec l'hébergement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..events import Event, EventType


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    """Un événement durable, numéroté et daté."""

    seq: int
    at: datetime
    event: Event

    def to_dict(self) -> dict[str, Any]:
        return {**self.event.to_dict(), "seq": self.seq, "ts": self.at.isoformat()}


@dataclass(slots=True)
class EventLog:
    """Journal append-only d'un salon, avec tampon des tours en cours."""

    _events: list[LoggedEvent] = field(default_factory=list)
    _seq: int = 0
    #: Texte accumulé par tour en cours, alimenté par les deltas. Vidé à la fin
    #: du tour, quand le message final devient l'enregistrement durable.
    _partials: dict[str, str] = field(default_factory=dict)

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(self, event: Event) -> LoggedEvent | None:
        """Enregistre un événement. Renvoie None s'il est éphémère.

        Les deltas alimentent le tampon du tour au lieu d'être écrits.
        """
        if not event.durable:
            if event.type is EventType.ASSISTANT_DELTA and event.turn_id:
                self._partials[event.turn_id] = (
                    self._partials.get(event.turn_id, "") + event.data.get("text", "")
                )
            return None

        # Le message final rend le tampon inutile : la suite se rejoue depuis le
        # journal.
        if event.type in (EventType.ASSISTANT_MESSAGE, EventType.TURN_ENDED) and event.turn_id:
            self._partials.pop(event.turn_id, None)

        self._seq += 1
        logged = LoggedEvent(seq=self._seq, at=datetime.now(UTC), event=event)
        self._events.append(logged)
        return logged

    def since(self, seq: int = 0) -> list[LoggedEvent]:
        """Événements strictement postérieurs à `seq`, dans l'ordre."""
        if seq <= 0:
            return list(self._events)
        return [e for e in self._events if e.seq > seq]

    def partials(self) -> dict[str, str]:
        """Texte déjà produit par les tours en cours, pour un arrivant tardif.

        Le client doit **remplacer** son tampon par cette valeur plutôt que d'y
        ajouter : sinon un client qui se reconnecte en plein tour duplique le
        texte qu'il avait déjà reçu.
        """
        return dict(self._partials)
