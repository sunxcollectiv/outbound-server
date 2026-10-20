"""reorder areas columns so parent_id sits after kind, before level

Purely cosmetic: lands the physical column order at
(id, name, kind, parent_id, level, photo_url, created_at, updated_at). Postgres
can't reorder in place, so the table is rebuilt — every column, including the
seeded parent_id links, is copied across. Nothing is discarded.

Revision ID: f1a2b3c4d5e6
Revises: e6a9b1c2d3f4
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e6a9b1c2d3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Column copy order is identical in both directions; only the CREATE differs.
_COPY = "id, name, kind, parent_id, level, photo_url, created_at, updated_at"


def _rebuild(columns: list[sa.Column]) -> None:
    # Detach both FKs that involve the areas table, swap it out, re-attach.
    op.drop_constraint(op.f('fk_areas_parent_id_areas'), 'areas', type_='foreignkey')
    op.drop_constraint(op.f('fk_places_area_id_areas'), 'places', type_='foreignkey')

    op.create_table('areas_new', *columns, sa.PrimaryKeyConstraint('id', name='pk_areas_new'))
    op.execute(f'INSERT INTO areas_new ({_COPY}) SELECT {_COPY} FROM areas')

    op.drop_table('areas')
    op.rename_table('areas_new', 'areas')
    op.execute('ALTER TABLE areas RENAME CONSTRAINT pk_areas_new TO pk_areas')

    op.create_index(op.f('ix_areas_name'), 'areas', ['name'], unique=False)
    op.create_index(op.f('ix_areas_kind'), 'areas', ['kind'], unique=False)
    op.create_index(op.f('ix_areas_parent_id'), 'areas', ['parent_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_areas_parent_id_areas'),
        'areas', 'areas', ['parent_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_places_area_id_areas'),
        'places', 'areas', ['area_id'], ['id'], ondelete='SET NULL',
    )


def upgrade() -> None:
    _rebuild([
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=60), nullable=True),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
        sa.Column('level', sa.Integer(), nullable=True),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ])


def downgrade() -> None:
    _rebuild([
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=60), nullable=True),
        sa.Column('level', sa.Integer(), nullable=True),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('parent_id', sa.Uuid(), nullable=True),
    ])
