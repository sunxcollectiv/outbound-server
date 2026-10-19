"""add transit segments

A transit booking can now hold several legs (a flight with layovers). The legs
live in trip_item_transit_segments, many to one back to the booking in
trip_items; the booking row keeps its overall span (first leg's departure, last
leg's arrival) so every cross-kind sort and the itinerary still read one row.

Each existing transit item becomes a single leg (position 0) copied straight
from its own columns, so a plain hop stays a one-leg booking.

Revision ID: c4e7f1a2b3d4
Revises: 09a2926adc2f
Create Date: 2026-10-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e7f1a2b3d4'
down_revision: Union[str, None] = '09a2926adc2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.String(length=64)
INSTANT = sa.DateTime(timezone=True)
LOCAL = sa.DateTime(timezone=False)


def upgrade() -> None:
    op.create_table(
        'trip_item_transit_segments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trip_item_id', sa.Uuid(), nullable=False),
        sa.Column('position', sa.SmallInteger(), nullable=False),
        sa.Column('starts_at', LOCAL, nullable=False),
        sa.Column('start_timezone', TZ, nullable=True),
        sa.Column('ends_at', LOCAL, nullable=True),
        sa.Column('end_timezone', TZ, nullable=True),
        sa.Column('starts_at_utc', INSTANT, nullable=True),
        sa.Column('ends_at_utc', INSTANT, nullable=True),
        sa.Column('place_id', sa.Uuid(), nullable=True),
        sa.Column('end_place_id', sa.Uuid(), nullable=True),
        sa.Column('carrier', sa.String(length=120), nullable=True),
        sa.Column('flight_number', sa.String(length=40), nullable=True),
        sa.Column('created_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['trip_item_id'], ['trip_items.id'], ondelete='CASCADE',
            name=op.f('fk_trip_item_transit_segments_trip_item_id_trip_items'),
        ),
        sa.ForeignKeyConstraint(
            ['place_id'], ['places.id'], ondelete='SET NULL',
            name=op.f('fk_trip_item_transit_segments_place_id_places'),
        ),
        sa.ForeignKeyConstraint(
            ['end_place_id'], ['places.id'], ondelete='SET NULL',
            name=op.f('fk_trip_item_transit_segments_end_place_id_places'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trip_item_transit_segments')),
        sa.UniqueConstraint(
            'trip_item_id', 'position', name='uq_transit_segment_position',
            deferrable=True, initially='DEFERRED',
        ),
        sa.CheckConstraint(
            'ends_at_utc IS NULL OR ends_at_utc >= starts_at_utc',
            name=op.f('ck_trip_item_transit_segments_instants_ordered'),
        ),
        sa.CheckConstraint(
            'start_timezone IS NOT NULL OR ends_at IS NULL OR ends_at >= starts_at',
            name=op.f('ck_trip_item_transit_segments_floating_ordered'),
        ),
        sa.CheckConstraint(
            '(start_timezone IS NOT NULL AND ends_at IS NOT NULL) = (end_timezone IS NOT NULL)'
            ' AND (start_timezone IS NULL) = (starts_at_utc IS NULL)'
            ' AND (end_timezone IS NULL) = (ends_at_utc IS NULL)',
            name=op.f('ck_trip_item_transit_segments_zones_consistent'),
        ),
    )
    op.create_index(
        'ix_transit_segments_item_position',
        'trip_item_transit_segments', ['trip_item_id', 'position'],
    )

    # One leg per existing transit booking, copied straight across.
    op.execute(
        "INSERT INTO trip_item_transit_segments ("
        "id, trip_item_id, position, starts_at, start_timezone, ends_at, end_timezone, "
        "starts_at_utc, ends_at_utc, place_id, end_place_id, created_at, updated_at) "
        "SELECT gen_random_uuid(), id, 0, starts_at, start_timezone, ends_at, end_timezone, "
        "starts_at_utc, ends_at_utc, place_id, end_place_id, now(), now() "
        "FROM trip_items WHERE kind = 'transit'"
    )


def downgrade() -> None:
    op.drop_index('ix_transit_segments_item_position', table_name='trip_item_transit_segments')
    op.drop_table('trip_item_transit_segments')
