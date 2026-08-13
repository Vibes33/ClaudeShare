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

Le stockage est derrière un **port** (`LogStore`) plutôt qu'en dur. Sans magasin,
tout vit en mémoire : c'est ce qu'il faut pour les tests et pour une session
locale jetable. Avec un magasin, le journal survit à un redémarrage — ce qui
n'est pas un luxe une fois le serveur hébergé, puisque le contexte de Claude,
lui, revient par `resume` : un modèle qui se souvient face à une interface qui a
tout oublié serait la pire des deux moitiés.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..events import Event, EventType

#: Nombre maximal d'événements rejoués d'un coup. Quelqu'un qui revient après
#: une semaine ne doit pas tirer tout l'historique du salon.
REPLAY_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    """Un événement durable, numéroté et daté."""

    seq: int
    at: datetime
    event: Event

    def to_dict(self) -> dict[str, Any]:
        return {**self.event.to_dict(), "seq": self.seq, "ts": self.at.isoformat()}


@dataclass(frozen=True, slots=True)
class Replay:
    """Ce qu'on renvoie à un client qui demande la suite depuis un `seq`."""

    events: list[dict[str, Any]] = field(default_factory=list)
    #: Des événements plus anciens ont été omis faute de place. Le client a donc
    #: un trou dans son historique — et il doit le savoir, sinon il affiche une
    #: conversation qui commence au milieu sans rien signaler. C'est exactement
    #: le défaut que le dédoublonnage sur `seq` sert à éviter par ailleurs.
    truncated: bool = False


class LogStore(Protocol):
    """Persistance du journal. Implémenté par `db/eventstore.py`."""

    def last_seq(self, room_id: str) -> int: ...

    def append(self, room_id: str, seq: int, event: Event, at: datetime) -> None: ...

    def since(self, room_id: str, seq: int, limit: int = REPLAY_LIMIT) -> Replay: ...


@dataclass(slots=True)
class EventLog:
    """Journal append-only d'un salon, avec tampon des tours en cours."""

    #: Identifiant du salon. Sert de clé dans le magasin ; sans magasin il n'est
    #: qu'une étiquette.
    room_id: str = "room"
    store: LogStore | None = None
    replay_limit: int = REPLAY_LIMIT
    #: Historique en mémoire. Vide dès qu'un magasin est branché — le garder en
    #: double ferait grossir le processus sans fin, ce qu'on vient justement de
    #: corriger.
    _events: list[LoggedEvent] = field(default_factory=list)
    _seq: int = 0
    #: Texte accumulé par tour en cours, alimenté par les deltas. Vidé à la fin
    #: du tour, quand le message final devient l'enregistrement durable.
    _partials: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Reprendre la numérotation là où le magasin l'a laissée. Repartir de 1
        # ferait réémettre des `seq` déjà vus, et les clients qui dédoublonnent
        # dessus jetteraient en silence tout ce qui suit un redémarrage.
        if self.store is not None:
            self._seq = self.store.last_seq(self.room_id)

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
        if self.store is not None:
            self.store.append(self.room_id, logged.seq, event, logged.at)
        else:
            self._events.append(logged)
        return logged

    def since(self, seq: int = 0) -> Replay:
        """Événements strictement postérieurs à `seq`, dans l'ordre."""
        if self.store is not None:
            return self.store.since(self.room_id, seq, self.replay_limit)

        events = self._events if seq <= 0 else [e for e in self._events if e.seq > seq]
        return Replay([e.to_dict() for e in events])

    def partials(self) -> dict[str, str]:
        """Texte déjà produit par les tours en cours, pour un arrivant tardif.

        Le client doit **remplacer** son tampon par cette valeur plutôt que d'y
        ajouter : sinon un client qui se reconnecte en plein tour duplique le
        texte qu'il avait déjà reçu.
        """
        return dict(self._partials)
