"""Accès à la base — SQLite en local, Postgres en déploiement.

Le schéma est le même des deux côtés : aucun type propre à un dialecte, `JSON`
plutôt que `JSONB`, des identifiants texte plutôt que des séquences. Ce n'est pas
de la neutralité pour la beauté du geste — c'est ce qui permet de développer sur
SQLite et de déployer sur Postgres sans découvrir la différence en production.

**Deux façons d'obtenir le schéma.** `create_all` pour les tests et le mode
local, où exécuter la chaîne de migrations à chaque base éphémère coûterait plus
que tout le reste ; les migrations Alembic en déploiement, seul endroit où une
migration ratée coûte quelque chose. Les deux chemins sont comparés par
`tests/test_migrations.py`, faute de quoi ils divergeraient à la première colonne
ajoutée sans révision.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

#: Connexions gardées ouvertes vers Postgres. Un salon actif en consomme peu —
#: les requêtes sont courtes — mais chaque intention WebSocket en prend une.
POOL_SIZE = 10
POOL_OVERFLOW = 20


class Schema(StrEnum):
    """Comment obtenir les tables au démarrage."""

    #: `create_all`. Rapide, sans historique : tests et session locale.
    CREATE = "create"
    #: `alembic upgrade head`. Le mode du déploiement.
    MIGRATE = "migrate"
    #: Ne rien faire — les migrations ont été appliquées séparément.
    NONE = "none"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_db_engine(url: str) -> Engine:
    """Moteur réglé pour le dialecte visé."""
    if is_sqlite(url):
        engine = create_engine(
            url,
            # SQLite refuse par défaut qu'une connexion serve plusieurs threads ;
            # le pool d'un serveur ASGI en a besoin.
            connect_args={"check_same_thread": False},
            future=True,
        )

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

    return create_engine(
        url,
        # Une connexion Postgres peut être coupée par un redémarrage ou un
        # pare-feu sans que le pool le sache. Sans ce test, la panne apparaît
        # sur une requête au hasard, longtemps après sa cause.
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_OVERFLOW,
        # Recyclage sous l'heure : la plupart des intermédiaires coupent les
        # connexions inactives passé un délai qu'on ne contrôle pas.
        pool_recycle=1800,
        future=True,
    )


def default_url(state_dir: Path) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{state_dir / 'claudeshare.db'}"


def normalize_url(url: str) -> str:
    """Ramène les variantes d'URL Postgres à un pilote installé.

    Les fournisseurs d'hébergement distribuent des `postgres://`, que SQLAlchemy
    ne reconnaît plus, et des `postgresql://` qui visent psycopg2 alors qu'on
    embarque psycopg 3. Corriger ici plutôt que d'exiger la bonne forme évite un
    échec au démarrage pour une variable copiée telle quelle.
    """
    for prefixe in ("postgres://", "postgresql://"):
        if url.startswith(prefixe):
            return "postgresql+psycopg://" + url[len(prefixe) :]
    return url


class Database:
    """Fabrique de sessions, initialisée une fois au démarrage."""

    def __init__(self, url: str, schema: Schema = Schema.CREATE) -> None:
        self.url = normalize_url(url)
        self.engine = create_db_engine(self.url)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

        match schema:
            case Schema.CREATE:
                Base.metadata.create_all(self.engine)
            case Schema.MIGRATE:
                from .migrate import upgrade

                upgrade(self.url)
            case Schema.NONE:
                pass

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
