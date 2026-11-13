"""store international_airport places as category airport

Google gives a commercial airport the primaryType "airport" or
"international_airport". We now store both as "airport" (see CATEGORY_ALIASES in
app/services/google_places.py), the category the seeded airports use, so this
brings places that were enriched before that change in line.

None of the affected rows has a code (only seeded airports do), so the unique
index over (category, code) cannot be violated.

The downgrade is a no-op: once merged, the rows that used to be
"international_airport" can't be told from the ones that were always "airport".

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-11-11 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE places SET category = 'airport' "
        "WHERE category = 'international_airport' AND code IS NULL"
    )


def downgrade() -> None:
    pass
