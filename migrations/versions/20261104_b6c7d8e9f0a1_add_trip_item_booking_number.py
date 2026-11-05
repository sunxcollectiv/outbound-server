"""add trip_items.booking_number

The reference of whoever an item was booked through when that isn't the
provider — a travel agent's or booking site's booking number — alongside the
provider's own confirmation_number. One agent booking can cover several items
(three hotels → three items sharing one booking_number, each with its hotel's
confirmation_number).

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b6c7d8e9f0a1'
down_revision: Union[str, None] = 'a5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trip_items', sa.Column('booking_number', sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column('trip_items', 'booking_number')
