"""a stay checks out on a later date than it checks in

Adds ck_trip_items_stay_spans_a_night: a lodging item's ends_at, when set, falls
on a later (local) date than its starts_at. Same-day check-in/check-out is
invalid; the API answers it with a 422, and the importer drops a same-day
check-out rather than failing the import.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, None] = 'e9f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Bare token: the naming convention renders ck_<table>_<token>.
    op.create_check_constraint(
        'stay_spans_a_night', 'trip_items',
        "kind <> 'lodging' OR ends_at IS NULL OR ends_at::date > starts_at::date",
    )


def downgrade() -> None:
    op.drop_constraint('stay_spans_a_night', 'trip_items', type_='check')
