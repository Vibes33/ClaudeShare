"""Identifiants Anthropic déposés par les profils.

Le secret n'est stocké que chiffré (`core/secretbox.py`). La colonne est
volontairement large : un jeton chiffré et encodé pèse plusieurs fois sa taille
d'origine, et une troncature silencieuse donnerait un identifiant illisible au
premier démarrage d'agent.

Revision ID: 0003_credentials
Revises: 0002_room_code
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_credentials"
down_revision: str | None = "0002_room_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('credentials',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('sealed', sa.String(length=2048), nullable=False),
    sa.Column('fingerprint', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_credentials_user_id'), ['user_id'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_credentials_user_id'))

    op.drop_table('credentials')
