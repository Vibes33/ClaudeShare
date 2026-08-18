"""Code de salon à sept chiffres.

Ajouté nullable : les salons existants n'en ont pas, et un code se demande
explicitement plutôt que d'apparaître un jour sur une conversation dont le
propriétaire ignorait qu'elle en avait un.

Revision ID: 0002_room_code
Revises: 0001_schema_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_room_code"
down_revision: str | None = "0001_schema_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.add_column(sa.Column('code', sa.String(length=16), nullable=True))
        batch_op.create_index(batch_op.f('ix_rooms_code'), ['code'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_rooms_code'))
        batch_op.drop_column('code')

