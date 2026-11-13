"""add places.code

A short public code for a place, whose meaning depends on the place's category:
for category "airport" it is the IATA code ("LAX"). Other categories can adopt
their own scheme later (a rail station code, say), which is why the column is
a generic `code` rather than `iata_code`.

A code identifies one place within its category, enforced by a partial unique
index over (category, code) for rows that have a code. That index also serves
the lookup by code, so there is no separate one.

Revision ID: b7c8d9e0f1a2
Revises: f5f9368e5e95
Create Date: 2026-11-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'f5f9368e5e95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('places', sa.Column('code', sa.String(length=20), nullable=True))
    op.create_index(
        'uq_places_category_code',
        'places',
        ['category', 'code'],
        unique=True,
        postgresql_where=sa.text('code IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_places_category_code', table_name='places')
    op.drop_column('places', 'code')
