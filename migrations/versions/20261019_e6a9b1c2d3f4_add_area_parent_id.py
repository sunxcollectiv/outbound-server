"""add area parent_id

A self-reference so an area can sit inside another (a city inside its country).
Self-referential on purpose: the hierarchy is arbitrary depth, so a province
layer can later slot between city and country without a schema change. Nullable;
cleared (SET NULL) if the parent area is deleted.

Revision ID: e6a9b1c2d3f4
Revises: d5f8a0c1b2e3
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e6a9b1c2d3f4'
down_revision: Union[str, None] = 'd5f8a0c1b2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('areas', sa.Column('parent_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_areas_parent_id'), 'areas', ['parent_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_areas_parent_id_areas'),
        'areas', 'areas', ['parent_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(op.f('fk_areas_parent_id_areas'), 'areas', type_='foreignkey')
    op.drop_index(op.f('ix_areas_parent_id'), table_name='areas')
    op.drop_column('areas', 'parent_id')
