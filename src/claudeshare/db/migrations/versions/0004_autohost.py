"""Intention d'hébergement d'un salon.

`False` pour les salons existants : personne n'a demandé leur hébergement
automatique, et le poser à `True` ferait démarrer des sessions Claude que
personne n'attend au premier lancement suivant.

Revision ID: 0004_autohost
Revises: 0003_credentials
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_autohost"
down_revision: str | None = "0003_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.add_column(sa.Column('autohost', sa.Boolean(), nullable=False, server_default=sa.false()))



def downgrade() -> None:
    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.drop_column('autohost')

