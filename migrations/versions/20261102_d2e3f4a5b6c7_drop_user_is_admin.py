"""drop users.is_admin; r_images.user_id nullable

Admin access is now service-token-only (see service_tokens); no user is ever an
admin, so the users.is_admin flag is gone. Image receipts uploaded through the
admin dev endpoints have no user behind them, so r_images.user_id becomes
nullable (the user-facing POST /received/images/process still sets it).

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('users', 'is_admin')
    op.alter_column('r_images', 'user_id', existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Restoring NOT NULL fails if any image receipts were uploaded via the admin
    # (service-token) endpoints, which leave user_id null — clean those up by
    # hand first if so (there's no user to backfill them to).
    op.alter_column('r_images', 'user_id', existing_type=sa.Uuid(), nullable=False)
    op.add_column(
        'users',
        sa.Column(
            'is_admin', sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
