"""add trip_item_lodgings

The first one-to-one, kind-specific child of trip_items: a stay's details beyond
its span (check-in/check-out) and place (the property) — what kind of property,
room type, guests, rooms, the property's phone and the free-cancellation
deadline. One row per lodging item, keyed by the item, deleted with it. Both the
mail/image importer and manual entry write it.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a5b6c7d8e9f0'
down_revision: Union[str, None] = 'f4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INSTANT = sa.DateTime(timezone=True)
_TYPES = ('hotel', 'vacation_rental', 'hostel', 'other')


def upgrade() -> None:
    op.create_table(
        'trip_item_lodgings',
        sa.Column('trip_item_id', sa.Uuid(), nullable=False),
        sa.Column(
            'property_type',
            sa.Enum(*_TYPES, name='lodging_type', native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column('room_type', sa.String(length=200), nullable=True),
        sa.Column('guests', sa.SmallInteger(), nullable=True),
        sa.Column('rooms', sa.SmallInteger(), nullable=True),
        sa.Column('phone', sa.String(length=40), nullable=True),
        sa.Column('free_cancellation_until', sa.DateTime(timezone=False), nullable=True),
        sa.Column('created_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['trip_item_id'], ['trip_items.id'], ondelete='CASCADE',
            name=op.f('fk_trip_item_lodgings_trip_item_id_trip_items'),
        ),
        sa.PrimaryKeyConstraint('trip_item_id', name=op.f('pk_trip_item_lodgings')),
        sa.CheckConstraint(
            "property_type IN ('hotel', 'vacation_rental', 'hostel', 'other')",
            name=op.f('ck_trip_item_lodgings_lodging_type'),
        ),
        sa.CheckConstraint(
            'guests IS NULL OR guests >= 1', name=op.f('ck_trip_item_lodgings_guests_positive')
        ),
        sa.CheckConstraint(
            'rooms IS NULL OR rooms >= 1', name=op.f('ck_trip_item_lodgings_rooms_positive')
        ),
    )


def downgrade() -> None:
    op.drop_table('trip_item_lodgings')
