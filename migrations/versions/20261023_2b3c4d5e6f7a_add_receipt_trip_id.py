"""add r_email_receipts.trip_id

Links a received email to the trip the import stage placed it into (set when the
receipt reaches `processed`). Nullable FK, ON DELETE SET NULL, indexed.

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '2b3c4d5e6f7a'
down_revision: Union[str, None] = '1a2b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('r_email_receipts', sa.Column('trip_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f('fk_r_email_receipts_trip_id_trips'),
        'r_email_receipts', 'trips', ['trip_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index(
        op.f('ix_r_email_receipts_trip_id'), 'r_email_receipts', ['trip_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_r_email_receipts_trip_id'), table_name='r_email_receipts')
    op.drop_constraint(
        op.f('fk_r_email_receipts_trip_id_trips'), 'r_email_receipts', type_='foreignkey'
    )
    op.drop_column('r_email_receipts', 'trip_id')
