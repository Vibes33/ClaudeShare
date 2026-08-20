"""Des rôles supplémentaires par appartenance.

Une personne portait un rôle ; elle peut désormais en porter plusieurs. Le rôle
principal reste où il était — il ancre la propriété du salon, donc le compte de
propriétaires qu'on refuse de faire tomber à zéro — et les autres s'ajoutent
dans cette colonne.

Colonne JSON avec un défaut serveur : sans lui, les appartenances existantes
auraient `NULL` là où le code attend une liste, et chaque lecture devrait s'en
souvenir. Un `[]` posé à la migration ferme la question une fois pour toutes.

Revision ID: 0008_extra_roles
Revises: 0007_room_bans
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_extra_roles"
down_revision: str | None = "0007_room_bans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Même précaution qu'en 0007, pour la même raison : une base locale peut
    # arriver ici par un chemin où la colonne existe déjà.
    colonnes = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memberships")}
    if "extra_role_ids" in colonnes:
        return

    op.add_column(
        "memberships",
        sa.Column("extra_role_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    colonnes = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memberships")}
    if "extra_role_ids" not in colonnes:
        return
    op.drop_column("memberships", "extra_role_ids")
