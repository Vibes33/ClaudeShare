"""Schéma initial.

Reprend l'état des modèles à la fin de l'étape 8, en une seule révision : le
projet n'avait pas encore de données déployées à préserver, donc rejouer
l'historique des huit étapes n'aurait décrit l'histoire de personne.

Revision ID: 0001_schema_initial
Revises: —
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_schema_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('subject', sa.String(length=128), nullable=False),
    sa.Column('handle', sa.String(length=128), nullable=False),
    sa.Column('display_name', sa.String(length=128), nullable=False),
    sa.Column('email', sa.String(length=256), nullable=True),
    sa.Column('avatar_url', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'subject', name='uq_user_provider_subject')
    )
    op.create_table('api_tokens',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_tokens_token_hash'), ['token_hash'], unique=True)

    op.create_table('rooms',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=128), nullable=False),
    sa.Column('workspace', sa.String(length=1024), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('session_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('turn_id', sa.String(length=64), nullable=True),
    sa.Column('author', sa.String(length=128), nullable=True),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('room_id', 'seq', name='uq_event_room_seq')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_room_id'), ['room_id'], unique=False)

    op.create_table('join_requests',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('roles',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('builtin', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('room_id', 'name', name='uq_role_room_name')
    )
    op.create_table('invitations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('identifier', sa.String(length=256), nullable=False),
    sa.Column('role_id', sa.String(length=40), nullable=False),
    sa.Column('invited_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_user_id', sa.String(length=40), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['accepted_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invitations_identifier'), ['identifier'], unique=False)

    op.create_table('invite_links',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('role_id', sa.String(length=40), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('max_uses', sa.Integer(), nullable=False),
    sa.Column('uses', sa.Integer(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('invite_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invite_links_token_hash'), ['token_hash'], unique=True)

    op.create_table('memberships',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('room_id', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('role_id', sa.String(length=40), nullable=False),
    sa.Column('grants', sa.JSON(), nullable=False),
    sa.Column('revokes', sa.JSON(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
    sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('room_id', 'user_id', name='uq_membership_room_user')
    )


def downgrade() -> None:
    op.drop_table('memberships')
    with op.batch_alter_table('invite_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invite_links_token_hash'))

    op.drop_table('invite_links')
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invitations_identifier'))

    op.drop_table('invitations')
    op.drop_table('roles')
    op.drop_table('join_requests')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_room_id'))

    op.drop_table('events')
    op.drop_table('rooms')
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_tokens_token_hash'))

    op.drop_table('api_tokens')
    op.drop_table('users')
