"""local times on trip items

Trip item times become naive local wall time plus, when known, an IANA zone
per end (start_timezone / end_timezone), the way a ticket prints them, with
derived starts_at_utc / ends_at_utc instants for the zoned ends. An item with
no zone is a floating time. The single optional `timezone` column goes away.

Existing rows keep the zone they had, their own `timezone` else their
place's, and their instant. Rows with neither become floating, with the UTC
reading of the old instant as their wall clock; that's what the old client
sent, so it's the time the user typed.

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

    # Keep each row's zone where it had one (its own, else its place's) and
    # its instant. end_timezone must be set before ends_at is converted.
    op.execute(
        "UPDATE trip_items SET start_timezone = COALESCE("
        "timezone, (SELECT timezone FROM places WHERE places.id = trip_items.place_id))"
    )
    op.execute(
        "UPDATE trip_items SET end_timezone = COALESCE("
        "(SELECT timezone FROM places WHERE places.id = trip_items.end_place_id), start_timezone) "
        "WHERE ends_at IS NOT NULL AND start_timezone IS NOT NULL"
    )
    op.execute(
        "UPDATE trip_items SET "
        "starts_at_utc = CASE WHEN start_timezone IS NOT NULL THEN starts_at END, "
        "ends_at_utc = CASE WHEN end_timezone IS NOT NULL THEN ends_at END"
    )

    # The old ordering check compares wall times, which can legitimately run
    # backwards across zones, and Postgres re-validates checks on a type change.
    op.drop_constraint(op.f('ck_trip_items_times_ordered'), 'trip_items', type_='check')
    op.alter_column(
        'trip_items', 'starts_at',
        existing_type=INSTANT, type_=LOCAL, existing_nullable=False,
        postgresql_using="starts_at AT TIME ZONE COALESCE(start_timezone, 'UTC')",
    )
    op.alter_column(
        'trip_items', 'ends_at',
        existing_type=INSTANT, type_=LOCAL, existing_nullable=True,
        postgresql_using="ends_at AT TIME ZONE COALESCE(end_timezone, 'UTC')",
    )
    op.create_check_constraint(
        op.f('ck_trip_items_instants_ordered'), 'trip_items',
        'ends_at_utc IS NULL OR ends_at_utc >= starts_at_utc',
    )
    op.create_check_constraint(
        op.f('ck_trip_items_floating_ordered'), 'trip_items',
        'start_timezone IS NOT NULL OR ends_at IS NULL OR ends_at >= starts_at',
    )
    op.create_check_constraint(
        op.f('ck_trip_items_zones_consistent'), 'trip_items',
        '(start_timezone IS NOT NULL AND ends_at IS NOT NULL) = (end_timezone IS NOT NULL)'
        ' AND (start_timezone IS NULL) = (starts_at_utc IS NULL)'
        ' AND (end_timezone IS NULL) = (ends_at_utc IS NULL)',
    )
    op.drop_column('trip_items', 'timezone')


def downgrade() -> None:
    op.add_column('trip_items', sa.Column('timezone', TZ, nullable=True))
    op.execute("UPDATE trip_items SET timezone = start_timezone")

    op.drop_constraint(op.f('ck_trip_items_zones_consistent'), 'trip_items', type_='check')
    op.drop_constraint(op.f('ck_trip_items_floating_ordered'), 'trip_items', type_='check')
    op.drop_constraint(op.f('ck_trip_items_instants_ordered'), 'trip_items', type_='check')
    # Zoned ends restore from their exact instants; floating ones are read as UTC.
    op.alter_column(
        'trip_items', 'starts_at',
        existing_type=LOCAL, type_=INSTANT, existing_nullable=False,
        postgresql_using="COALESCE(starts_at_utc, starts_at AT TIME ZONE 'UTC')",
    )
    op.alter_column(
        'trip_items', 'ends_at',
        existing_type=LOCAL, type_=INSTANT, existing_nullable=True,
        postgresql_using="COALESCE(ends_at_utc, ends_at AT TIME ZONE 'UTC')",
    )
    op.create_check_constraint(
        op.f('ck_trip_items_times_ordered'), 'trip_items',
        'ends_at IS NULL OR ends_at >= starts_at',
    )

    op.drop_column('trip_items', 'ends_at_utc')
    op.drop_column('trip_items', 'starts_at_utc')
    op.drop_column('trip_items', 'end_timezone')
    op.drop_column('trip_items', 'start_timezone')
