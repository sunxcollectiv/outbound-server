"""local times on trip items

Trip item times become naive local wall time plus an IANA zone per end
(start_timezone / end_timezone), the way a ticket prints them, with derived
starts_at_utc / ends_at_utc instants for ordering. The single optional
`timezone` column goes away.

Existing rows keep their instant and are labelled with the zone they had:
their own `timezone`, else their place's, else UTC. Rows that end up as UTC
were never given a zone, so their wall clocks are the UTC reading of the
stored instant, not the local time the user meant; those need re-entering.

Revision ID: 3a3604a816ed
Revises: b3f1c2d4e5a6
Create Date: 2026-10-15 13:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3a3604a816ed'
down_revision: Union[str, None] = 'b3f1c2d4e5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.String(length=64)
INSTANT = sa.DateTime(timezone=True)
LOCAL = sa.DateTime(timezone=False)


def upgrade() -> None:
    op.add_column('trip_items', sa.Column('start_timezone', TZ, nullable=True))
    op.add_column('trip_items', sa.Column('end_timezone', TZ, nullable=True))
    op.add_column('trip_items', sa.Column('starts_at_utc', INSTANT, nullable=True))
    op.add_column('trip_items', sa.Column('ends_at_utc', INSTANT, nullable=True))

    # Label every row with the zone it was being bucketed in, then keep the
    # instant it already had. end_timezone must be set before ends_at is
    # converted: AT TIME ZONE NULL would null the column.
    op.execute(
        "UPDATE trip_items SET start_timezone = COALESCE("
        "timezone, (SELECT timezone FROM places WHERE places.id = trip_items.place_id), 'UTC')"
    )
    op.execute(
        "UPDATE trip_items SET end_timezone = COALESCE("
        "(SELECT timezone FROM places WHERE places.id = trip_items.end_place_id), start_timezone) "
        "WHERE ends_at IS NOT NULL"
    )
    op.execute("UPDATE trip_items SET starts_at_utc = starts_at, ends_at_utc = ends_at")
    op.alter_column('trip_items', 'start_timezone', existing_type=TZ, nullable=False)
    op.alter_column('trip_items', 'starts_at_utc', existing_type=INSTANT, nullable=False)

    # The old ordering check compares wall times, which can legitimately run
    # backwards across zones, and Postgres re-validates checks on a type change.
    op.drop_constraint(op.f('ck_trip_items_times_ordered'), 'trip_items', type_='check')
    op.drop_index('ix_trip_items_trip_id_starts_at', table_name='trip_items')
    op.alter_column(
        'trip_items', 'starts_at',
        existing_type=INSTANT, type_=LOCAL, existing_nullable=False,
        postgresql_using='starts_at AT TIME ZONE start_timezone',
    )
    op.alter_column(
        'trip_items', 'ends_at',
        existing_type=INSTANT, type_=LOCAL, existing_nullable=True,
        postgresql_using='ends_at AT TIME ZONE end_timezone',
    )
    op.create_check_constraint(
        op.f('ck_trip_items_instants_ordered'), 'trip_items',
        'ends_at_utc IS NULL OR ends_at_utc >= starts_at_utc',
    )
    op.create_check_constraint(
        op.f('ck_trip_items_end_has_timezone'), 'trip_items',
        '(ends_at IS NULL) = (end_timezone IS NULL) AND (ends_at IS NULL) = (ends_at_utc IS NULL)',
    )
    op.create_index(
        'ix_trip_items_trip_id_starts_at_utc', 'trip_items', ['trip_id', 'starts_at_utc'],
        unique=False,
    )
    op.drop_column('trip_items', 'timezone')


def downgrade() -> None:
    op.add_column('trip_items', sa.Column('timezone', TZ, nullable=True))
    op.execute("UPDATE trip_items SET timezone = start_timezone")

    op.drop_index('ix_trip_items_trip_id_starts_at_utc', table_name='trip_items')
    op.drop_constraint(op.f('ck_trip_items_end_has_timezone'), 'trip_items', type_='check')
    op.drop_constraint(op.f('ck_trip_items_instants_ordered'), 'trip_items', type_='check')
    # The stored instants are exact, so restore from them rather than re-deriving.
    op.alter_column(
        'trip_items', 'starts_at',
        existing_type=LOCAL, type_=INSTANT, existing_nullable=False,
        postgresql_using='starts_at_utc',
    )
    op.alter_column(
        'trip_items', 'ends_at',
        existing_type=LOCAL, type_=INSTANT, existing_nullable=True,
        postgresql_using='ends_at_utc',
    )
    op.create_check_constraint(
        op.f('ck_trip_items_times_ordered'), 'trip_items',
        'ends_at IS NULL OR ends_at >= starts_at',
    )
    op.create_index(
        'ix_trip_items_trip_id_starts_at', 'trip_items', ['trip_id', 'starts_at'], unique=False
    )

    op.drop_column('trip_items', 'ends_at_utc')
    op.drop_column('trip_items', 'starts_at_utc')
    op.drop_column('trip_items', 'end_timezone')
    op.drop_column('trip_items', 'start_timezone')
