"""Accès à la base.

SQLite en v1, sur le schéma que Postgres reprendra tel quel. Les tables sont
créées au démarrage plutôt que par migrations : le projet n'a pas encore de
données à préserver, et Alembic viendra avec l'hébergement (étape 9), au moment
où une migration ratée coûterait quelque chose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def create_db_engine(url: str) -> Engine:
    """Moteur configuré pour un usage concurrent raisonnable en SQLite."""
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        # SQLite refuse par défaut qu'une connexion serve plusieurs threads ;
        # le pool d'un serveur ASGI en a besoin.
        connect_args={"check_same_thread": False} if is_sqlite else {},
        future=True,
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            # WAL : un lecteur ne bloque pas un écrivain. Sans ça, un salon actif
            # verrouille la base pour les autres.
            cursor.execute("PRAGMA journal_mode=WAL")
            # SQLite n'applique pas les clés étrangères par défaut — or le schéma
            # s'appuie dessus pour les suppressions en cascade.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def default_url(state_dir: Path) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{state_dir / 'claudeshare.db'}"


class Database:
    """Fabrique de sessions, initialisée une fois au démarrage."""

    def __init__(self, url: str) -> None:
        self.engine = create_db_engine(url)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Session transactionnelle : commit au succès, rollback à l'échec."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
