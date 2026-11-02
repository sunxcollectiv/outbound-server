"""service_tokens: machine-to-machine admin credentials

A bearer token not tied to any user, for the admin console (wravel-manage) to
call the /admin APIs. Resolved to a service principal (no User) with its own
admin flag; null expires_at means it never expires.

Revision ID: c1d2e3f4a5b6
Revises: 8b9c0d1e2f3a
Create Date: 2026-11-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = '8b9c0d1e2f3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_tokens',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('is_admin', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_service_tokens')),
        sa.UniqueConstraint('token_hash', name=op.f('uq_service_tokens_token_hash')),
    )


def downgrade() -> None:
    op.drop_table('service_tokens')
