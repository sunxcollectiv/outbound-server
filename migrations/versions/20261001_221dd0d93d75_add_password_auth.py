"""add password auth

Revision ID: 221dd0d93d75
Revises: 7c46bb2a5f44
Create Date: 2026-10-01 23:56:53.426721

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '221dd0d93d75'
down_revision: Union[str, None] = '7c46bb2a5f44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('access_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_access_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_access_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_access_tokens_token_hash'))
    )
    op.create_index(op.f('ix_access_tokens_user_id'), 'access_tokens', ['user_id'], unique=False)
    # No default: users created before this have no credential to backfill,
    # so the upgrade expects an empty users table.
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'password_hash')
    op.drop_index(op.f('ix_access_tokens_user_id'), table_name='access_tokens')
    op.drop_table('access_tokens')
