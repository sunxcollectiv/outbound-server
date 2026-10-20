"""add area kind and level

Areas grow beyond cities: a free-form `kind` string (country, city, region,
neighborhood, borough, ...) and a numeric `level` for how broad/narrow the area
is, to drive prioritization and nesting later. Both nullable; existing rows
(mostly system-derived cities) keep NULL until backfilled.

The table is rebuilt so the columns land in a readable order
(id, name, kind, level, photo_url, created_at, updated_at) rather than having
the new columns tacked on at the end — Postgres can't reorder in place. All
existing rows are copied across; nothing is discarded.

Revision ID: d5f8a0c1b2e3
Revises: c4e7f1a2b3d4
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5f8a0c1b2e3'
down_revision: Union[str, None] = 'c4e7f1a2b3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Detach the places FK so the areas table can be swapped out.
    op.drop_constraint(op.f('fk_places_area_id_areas'), 'places', type_='foreignkey')

    # Build the replacement with the desired column order.
    op.create_table(
        'areas_new',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=60), nullable=True),
        sa.Column('level', sa.Integer(), nullable=True),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_areas_new'),
    )
    op.execute(
        'INSERT INTO areas_new (id, name, photo_url, created_at, updated_at) '
        'SELECT id, name, photo_url, created_at, updated_at FROM areas'
    )

    op.drop_table('areas')
    op.rename_table('areas_new', 'areas')
    op.execute('ALTER TABLE areas RENAME CONSTRAINT pk_areas_new TO pk_areas')
    op.create_index(op.f('ix_areas_name'), 'areas', ['name'], unique=False)
    op.create_index(op.f('ix_areas_kind'), 'areas', ['kind'], unique=False)
    op.create_foreign_key(
        op.f('fk_places_area_id_areas'),
        'places', 'areas', ['area_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    # Reverse the swap: drop kind/level and restore the prior column order.
    op.drop_constraint(op.f('fk_places_area_id_areas'), 'places', type_='foreignkey')

    op.create_table(
        'areas_old',
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_areas_old'),
    )
    op.execute(
        'INSERT INTO areas_old (id, name, photo_url, created_at, updated_at) '
        'SELECT id, name, photo_url, created_at, updated_at FROM areas'
    )

    op.drop_table('areas')
    op.rename_table('areas_old', 'areas')
    op.execute('ALTER TABLE areas RENAME CONSTRAINT pk_areas_old TO pk_areas')
    op.create_index(op.f('ix_areas_name'), 'areas', ['name'], unique=False)
    op.create_foreign_key(
        op.f('fk_places_area_id_areas'),
        'places', 'areas', ['area_id'], ['id'], ondelete='SET NULL',
    )
