"""a stay always has a check-out, at least one night after its check-in

Tightens ck_trip_items_stay_spans_a_night: a lodging item's ends_at is now
required (previously it could be null) and still falls on a later local date
than its starts_at. The API answers a missing or same-day check-out with a 422;
the importer assumes one night (check-out 11:00 the next day) when a booking
states none or a same-day one.

Fails if a stay with no check-out exists; none did in dev when written.

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REQUIRED = "kind <> 'lodging' OR (ends_at IS NOT NULL AND ends_at::date > starts_at::date)"
_OPTIONAL = "kind <> 'lodging' OR ends_at IS NULL OR ends_at::date > starts_at::date"


def upgrade() -> None:
    # Bare token on both: the naming convention renders ck_<table>_<token>.
    op.drop_constraint('stay_spans_a_night', 'trip_items', type_='check')
    op.create_check_constraint('stay_spans_a_night', 'trip_items', _REQUIRED)


def downgrade() -> None:
    op.drop_constraint('stay_spans_a_night', 'trip_items', type_='check')
    op.create_check_constraint('stay_spans_a_night', 'trip_items', _OPTIONAL)
