"""add transit_mode (item + segment) and per-segment confirmation_number

A transit leg's transport mode lives on the segment (so a booking can mix modes,
e.g. ferry + connecting bus); the item carries the overall mode (the shared value
or `mixed`). Segments also get their own optional confirmation_number for
separately-ticketed legs. Both the extractor and manual entry target this shape.

Revision ID: e8f1a2b3c4d5
Revises: d7e0a1b2c3f4
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e8f1a2b3c4d5'
down_revision: Union[str, None] = 'd7e0a1b2c3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODE_VALUES = ('flight', 'train', 'bus', 'ferry', 'car_rental', 'other', 'mixed')
_MODE_IN = "transit_mode IN ('flight', 'train', 'bus', 'ferry', 'car_rental', 'other', 'mixed')"


def _mode_column():
    return sa.Column(
        'transit_mode',
        sa.Enum(*_MODE_VALUES, name='transit_mode', native_enum=False, length=32),
        nullable=True,
    )


def upgrade() -> None:
    op.add_column('trip_items', _mode_column())
    op.add_column('trip_item_transit_segments', _mode_column())
    op.add_column(
        'trip_item_transit_segments',
        sa.Column('confirmation_number', sa.String(length=120), nullable=True),
    )
    # Pass the bare token; the naming convention renders ck_<table>_<token>.
    op.create_check_constraint('transit_mode', 'trip_items', _MODE_IN)
    op.create_check_constraint(
        'transit_mode_only_transit', 'trip_items', "transit_mode IS NULL OR kind = 'transit'"
    )
    op.create_check_constraint('transit_mode', 'trip_item_transit_segments', _MODE_IN)


def downgrade() -> None:
    # drop_constraint also applies the naming convention, so pass the bare token
    # (same as create), not the rendered ck_<table>_<token> name.
    op.drop_constraint('transit_mode', 'trip_item_transit_segments', type_='check')
    op.drop_constraint('transit_mode_only_transit', 'trip_items', type_='check')
    op.drop_constraint('transit_mode', 'trip_items', type_='check')
    op.drop_column('trip_item_transit_segments', 'confirmation_number')
    op.drop_column('trip_item_transit_segments', 'transit_mode')
    op.drop_column('trip_items', 'transit_mode')
