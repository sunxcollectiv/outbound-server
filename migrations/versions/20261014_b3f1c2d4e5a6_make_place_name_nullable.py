"""make place name nullable

Places are now registered as un-enriched stubs from just a Google place id;
the name (and everything else) is filled in by a later enrichment/review step,
so name can no longer be NOT NULL.

Revision ID: b3f1c2d4e5a6
Revises: 71661082a7a8
Create Date: 2026-10-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3f1c2d4e5a6'
down_revision: Union[str, None] = '71661082a7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('places', 'name', existing_type=sa.String(length=200), nullable=True)


def downgrade() -> None:
    op.alter_column('places', 'name', existing_type=sa.String(length=200), nullable=False)
