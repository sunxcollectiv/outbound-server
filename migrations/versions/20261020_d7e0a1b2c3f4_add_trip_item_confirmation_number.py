"""add trip_items.confirmation_number

Booking reference / record locator for a trip item (flight PNR, hotel
confirmation, etc.). A booking's own field — shared by manual entry and the
email extractor, which both target this column.

Revision ID: d7e0a1b2c3f4
Revises: c5d9e2f3a4b6
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7e0a1b2c3f4'
down_revision: Union[str, None] = 'c5d9e2f3a4b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trip_items', sa.Column('confirmation_number', sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column('trip_items', 'confirmation_number')
