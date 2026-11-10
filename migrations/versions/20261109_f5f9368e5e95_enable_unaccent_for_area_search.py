"""enable unaccent for area search

Installs Postgres's `unaccent` extension so area search can ignore accents:
`GET /areas?q=krakow` finds "Kraków". The search folds both the name and the
term with lower(unaccent(...)) at query time (app/api/areas.py).

No index and no stored folded column: the search is a substring match, which a
btree can't serve anyway, and at ~1,700 areas the sequential scan with the
folding takes under a millisecond. If the table grows enough to matter, the
route is a pg_trgm GIN index over an IMMUTABLE wrapper around unaccent (which
is only STABLE, so it can't be indexed directly).

`unaccent` ships with Postgres (contrib) and is a trusted extension, so the
database owner can create it without superuser.

Revision ID: f5f9368e5e95
Revises: a1b2c3d4e5f6
Create Date: 2026-11-09 19:48:14.106368

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5f9368e5e95'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS unaccent")
