"""add trip days

One row per calendar date of a trip: the scaffolding (zones, a place, a
name) for a day before anything is booked. Every existing trip gets a row
per date, with no zones, so the invariant holds from the start.

Revision ID: 09a2926adc2f
Revises: 3a3604a816ed
Create Date: 2026-10-15 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '09a2926adc2f'
down_revision: Union[str, None] = '3a3604a816ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trip_days',
        sa.Column('trip_id', sa.Uuid(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('timezones', postgresql.ARRAY(sa.String(length=64)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column('place_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['place_id'], ['places.id'], name=op.f('fk_trip_days_place_id_places'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], name=op.f('fk_trip_days_trip_id_trips'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('trip_id', 'date', name=op.f('pk_trip_days')),
    )
    op.create_index(op.f('ix_trip_days_place_id'), 'trip_days', ['place_id'], unique=False)
    op.execute(
        "INSERT INTO trip_days (trip_id, date) "
        "SELECT id, generate_series(start_date, end_date, interval '1 day')::date FROM trips"
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_trip_days_place_id'), table_name='trip_days')
    op.drop_table('trip_days')
