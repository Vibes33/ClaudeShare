"""Persistance du journal de collaboration — l'adaptateur du port `LogStore`.

Le journal était en mémoire jusqu'ici, et c'était tenable tant que « déployer »
voulait dire « lancer le serveur sur son poste ». Ça ne l'est plus : un serveur
qui redémarre perdait toute la conversation partagée, alors que le contexte de
Claude, lui, revenait par `resume`.

Le magasin est **synchrone**, appelé depuis la boucle asyncio. C'est l'arbitrage
déjà retenu partout ailleurs dans le serveur — `ctx.db.session()` est appelé
dans les routes et à chaque intention WebSocket. Une écriture indexée coûte une
fraction de milliseconde, et un pont vers un exécuteur coûterait plus cher en
complexité qu'il ne rapporterait.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select

from ..core.eventlog import REPLAY_LIMIT, Replay
from ..events import Event as DomainEvent
from .models import Event as EventRow
from .session import Database

logger = logging.getLogger(__name__)


class DatabaseLogStore:
    """Journal adossé à la base — SQLite en local, Postgres en déploiement."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def last_seq(self, room_id: str) -> int:
        with self._db.session() as session:
            return (
                session.scalar(
                    select(func.max(EventRow.seq)).where(EventRow.room_id == room_id)
                )
                or 0
            )

    def append(self, room_id: str, seq: int, event: DomainEvent, at: datetime) -> None:
        with self._db.session() as session:
            session.add(
                EventRow(
                    room_id=room_id,
                    seq=seq,
                    type=str(event.type),
                    turn_id=event.turn_id,
                    author=event.author,
                    data=event.data,
                    created_at=at,
                )
            )

    def since(self, room_id: str, seq: int, limit: int = REPLAY_LIMIT) -> Replay:
        """Événements postérieurs à `seq`, les plus anciens d'abord.

        Quand il y en a plus que la limite, la coupe garde les plus **récents** —
        c'est la fin de la conversation qui intéresse quelqu'un qui revient — et
        le `truncated` prévient qu'il manque le début.
        """
        with self._db.session() as session:
            filtre = (EventRow.room_id == room_id, EventRow.seq > seq)
            total = session.scalar(select(func.count()).select_from(EventRow).where(*filtre)) or 0
            lignes = list(
                session.scalars(
                    select(EventRow).where(*filtre).order_by(EventRow.seq.desc()).limit(limit)
                )
            )
        return Replay(
            events=[_to_dict(ligne) for ligne in reversed(lignes)],
            truncated=total > len(lignes),
        )

    def purge(self, room_id: str, keep: int) -> int:
        """Ne conserve que les `keep` derniers événements d'un salon.

        Sans rétention, un salon de longue vie fait croître la base sans borne.
        Appelé par l'entretien périodique, jamais sur le chemin d'écriture : la
        rétention n'a aucune raison de coûter quelque chose à chaque événement.
        """
        with self._db.session() as session:
            seuil = session.scalar(
                select(EventRow.seq)
                .where(EventRow.room_id == room_id)
                .order_by(EventRow.seq.desc())
                .offset(keep)
                .limit(1)
            )
            if seuil is None:
                return 0
            efface = session.execute(
                delete(EventRow).where(EventRow.room_id == room_id, EventRow.seq <= seuil)
            )
            return efface.rowcount or 0


def _to_dict(ligne: EventRow) -> dict[str, Any]:
    """Même forme que `LoggedEvent.to_dict()` : un client ne doit pas pouvoir
    distinguer un événement relu d'un événement gardé en mémoire."""
    return {
        "type": ligne.type,
        "turn_id": ligne.turn_id,
        "author": ligne.author,
        **(ligne.data or {}),
        "seq": ligne.seq,
        "ts": _aware(ligne.created_at).isoformat(),
    }


def _aware(moment: datetime) -> datetime:
    """SQLite rend des datetimes naïfs. Les traiter comme UTC, faute de quoi
    l'horodatage rejoué serait décalé du fuseau du serveur."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
