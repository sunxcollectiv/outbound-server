"""move a stay's room type / guests / room count into its rooms

Rooms (and participants) are now the one place a stay's rooms and people live,
editable like everything else on an imported item. So trip_item_lodgings drops
room_type, guests and room_count. Nothing is lost: a stay with any of them set
and no rooms yet gets that many rooms (room_count, else one; capped at 20) of
that room type, the guest count going on the room when there's just one.

Downgrade puts the columns back and refills them from the rooms (count, first
room's type, total guests); the rooms themselves are left in place.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO trip_item_lodging_rooms (id, trip_item_id, position, room_type, guests)
        SELECT gen_random_uuid(), l.trip_item_id, n.i - 1, l.room_type,
               CASE WHEN COALESCE(l.room_count, 1) = 1 THEN l.guests END
        FROM trip_item_lodgings l
        CROSS JOIN LATERAL generate_series(1, LEAST(COALESCE(l.room_count, 1), 20)) AS n(i)
        WHERE (l.room_type IS NOT NULL OR l.guests IS NOT NULL OR l.room_count IS NOT NULL)
          AND NOT EXISTS (
              SELECT 1 FROM trip_item_lodging_rooms r WHERE r.trip_item_id = l.trip_item_id
          )
        """
    )
    # Dropping a column drops the CHECK constraints on it (guests_positive,
    # room_count_positive) with it.
    op.drop_column('trip_item_lodgings', 'room_count')
    op.drop_column('trip_item_lodgings', 'guests')
    op.drop_column('trip_item_lodgings', 'room_type')


def downgrade() -> None:
    op.add_column('trip_item_lodgings', sa.Column('room_type', sa.String(length=200), nullable=True))
    op.add_column('trip_item_lodgings', sa.Column('guests', sa.SmallInteger(), nullable=True))
    op.add_column('trip_item_lodgings', sa.Column('room_count', sa.SmallInteger(), nullable=True))
    op.create_check_constraint('guests_positive', 'trip_item_lodgings', 'guests IS NULL OR guests >= 1')
    op.create_check_constraint(
        'room_count_positive', 'trip_item_lodgings', 'room_count IS NULL OR room_count >= 1'
    )
    op.execute(
        """
        UPDATE trip_item_lodgings l
        SET room_count = r.n, room_type = r.first_type, guests = NULLIF(LEAST(r.total, 999), 0)
        FROM (
            SELECT trip_item_id, count(*) AS n,
                   (array_agg(room_type ORDER BY position))[1] AS first_type,
                   COALESCE(sum(guests), 0) AS total
            FROM trip_item_lodging_rooms GROUP BY trip_item_id
        ) r
        WHERE r.trip_item_id = l.trip_item_id
        """
    )
