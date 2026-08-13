"""Point d'entrée Alembic.

L'URL n'est jamais lue dans un fichier : elle est fournie par `db/migrate.py`,
qui la tient de la configuration du serveur. Une base de production épinglée
dans un ini versionné est un accident qui attend son heure.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from claudeshare.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite ne sait pas modifier une colonne en place : Alembic
            # recrée la table. Sans ce mode, toute migration autre qu'un
            # simple `CREATE` échouerait sur le déploiement local.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
