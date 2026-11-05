"""add lodging rooms + participant room assignments

A stay can optionally spell out its rooms (trip_item_lodging_rooms: label, room
number, type, guests, a per-room confirmation number), and each participant can
optionally be put in one of them (trip_item_participants.room_id).

Two foreign keys guard the assignment:
- room_id -> rooms.id ON DELETE SET NULL: deleting a room unassigns its people
  (they stay on the stay);
- (item_id, room_id) -> rooms(trip_item_id, id), deferred: a participant's room
  must belong to the same stay. A null room_id satisfies it (MATCH SIMPLE).

Also renames trip_item_lodgings.rooms -> room_count (the booking's summary
count), so it doesn't read as the new per-room list.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b6c7d8e9f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INSTANT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.alter_column('trip_item_lodgings', 'rooms', new_column_name='room_count')
    op.execute(
        'ALTER TABLE trip_item_lodgings RENAME CONSTRAINT '
        'ck_trip_item_lodgings_rooms_positive TO ck_trip_item_lodgings_room_count_positive'
    )

    op.create_table(
        'trip_item_lodging_rooms',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('trip_item_id', sa.Uuid(), nullable=False),
        sa.Column('position', sa.SmallInteger(), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=True),
        sa.Column('room_number', sa.String(length=40), nullable=True),
        sa.Column('room_type', sa.String(length=200), nullable=True),
        sa.Column('guests', sa.SmallInteger(), nullable=True),
        sa.Column('confirmation_number', sa.String(length=120), nullable=True),
        sa.Column('created_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', INSTANT, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['trip_item_id'], ['trip_items.id'], ondelete='CASCADE',
            name=op.f('fk_trip_item_lodging_rooms_trip_item_id_trip_items'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trip_item_lodging_rooms')),
        sa.UniqueConstraint(
            'trip_item_id', 'position', name='uq_lodging_room_position',
            deferrable=True, initially='DEFERRED',
        ),
        sa.UniqueConstraint('trip_item_id', 'id', name='uq_lodging_room_item'),
        sa.CheckConstraint(
            'guests IS NULL OR guests >= 1',
            name=op.f('ck_trip_item_lodging_rooms_guests_positive'),
        ),
    )

    op.add_column('trip_item_participants', sa.Column('room_id', sa.Uuid(), nullable=True))
    op.create_index(
        op.f('ix_trip_item_participants_room_id'), 'trip_item_participants', ['room_id']
    )
    op.create_foreign_key(
        op.f('fk_trip_item_participants_room_id_trip_item_lodging_rooms'),
        'trip_item_participants', 'trip_item_lodging_rooms',
        ['room_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_trip_item_participants_item_id_trip_item_lodging_rooms'),
        'trip_item_participants', 'trip_item_lodging_rooms',
        ['item_id', 'room_id'], ['trip_item_id', 'id'],
        deferrable=True, initially='DEFERRED',
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('fk_trip_item_participants_item_id_trip_item_lodging_rooms'),
        'trip_item_participants', type_='foreignkey',
    )
    op.drop_constraint(
        op.f('fk_trip_item_participants_room_id_trip_item_lodging_rooms'),
        'trip_item_participants', type_='foreignkey',
    )
    op.drop_index(op.f('ix_trip_item_participants_room_id'), table_name='trip_item_participants')
    op.drop_column('trip_item_participants', 'room_id')
    op.drop_table('trip_item_lodging_rooms')
    op.execute(
        'ALTER TABLE trip_item_lodgings RENAME CONSTRAINT '
        'ck_trip_item_lodgings_room_count_positive TO ck_trip_item_lodgings_rooms_positive'
    )
    op.alter_column('trip_item_lodgings', 'room_count', new_column_name='rooms')
