"""Migrations Alembic, pilotées depuis le code.

Pas d'`alembic.ini` à la racine : la configuration est construite ici. Un fichier
ini oblige à raisonner sur un chemin relatif au dossier courant, et le dossier
courant d'un conteneur n'est pas celui d'un poste de développement — c'est le
genre de détail qui ne se découvre qu'au premier déploiement.

**Deux façons d'obtenir le schéma, et une seule fait autorité.** `create_all`
reste utilisé par les tests et le mode local, parce qu'exécuter la chaîne de
migrations à chaque base éphémère coûterait plus que tout le reste de la suite.
Les migrations, elles, sont ce qui s'applique en déploiement. Deux chemins vers
le même schéma se mettent à diverger dès qu'on ajoute une colonne sans écrire la
révision correspondante — d'où `tests/test_migrations.py`, qui construit le
schéma des deux façons et compare.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Échappement des `%` : Alembic passe la valeur par ConfigParser, et un mot
    # de passe Postgres encodé en pourcent y serait interprété comme une
    # interpolation.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """Applique les migrations manquantes."""
    logger.info("migrations : %s → %s", current(url) or "base vide", revision)
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def current(url: str) -> str | None:
    """Révision appliquée à cette base, ou None si aucune."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def head() -> str:
    """Dernière révision connue du code."""
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def pending(url: str) -> bool:
    """La base est-elle en retard sur le code ?

    Sert au démarrage : lancer un serveur sur un schéma périmé produit des
    erreurs SQL au premier appel, très loin de leur cause.
    """
    return current(url) != head()
