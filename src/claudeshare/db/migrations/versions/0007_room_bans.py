"""La table des exclusions.

Nouvelle table, donc une migration ordinaire — contrairement à 0005 et 0006 qui
remaniaient des données existantes. Elle est ici pour que `create_all` (local et
tests) et Alembic (déploiement) restent d'accord ; `tests/test_migrations.py`
construit les deux schémas et les compare.

Revision ID: 0007_room_bans
Revises: 0006_room_chat
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_room_bans"
down_revision: str | None = "0006_room_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotente, et ce n'est pas de la coquetterie. En local, le schéma vient
    # de `create_all`, qui crée les **tables** manquantes mais n'ajoute jamais de
    # colonne. Une base locale mise à jour a donc déjà `room_bans` — créée au
    # démarrage — mais pas `memberships.extra_role_ids`. Le garde-fou de schéma
    # renvoie alors vers `claudeshare migrate`, qui échouerait ici sur une table
    # déjà là, et laisserait la personne sans issue.
    if sa.inspect(op.get_bind()).has_table("room_bans"):
        return

    op.create_table(
        "room_bans",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "room_id",
            sa.String(40),
            sa.ForeignKey("rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(40),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `None` = définitive. C'est la lecture qui décide si une échéance passée
        # s'applique encore, pas une tâche de nettoyage.
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        # `SET NULL` et non `CASCADE` : la sanction survit à la disparition de
        # qui l'a prononcée. L'effacer avec son auteur perdrait l'exclusion.
        sa.Column(
            "by_user_id",
            sa.String(40),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room_id", "user_id", name="uq_ban_room_user"),
    )
    op.create_index("ix_room_bans_room_id", "room_bans", ["room_id"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("room_bans"):
        return
    op.drop_index("ix_room_bans_room_id", table_name="room_bans")
    op.drop_table("room_bans")
