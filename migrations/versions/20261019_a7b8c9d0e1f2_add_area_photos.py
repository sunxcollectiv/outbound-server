"""add area_photos table and rename areas.photo_url to default_photo_url

Areas get their own curated photos (sourced from Unsplash, hotlinked). The
single `photo_url` column becomes `default_photo_url` — the area's cover image,
usually the first of the new `area_photos` rows. Each area_photos row keeps the
provider credit (`source`, `attribution`, `attribution_url`) the provider's
terms require, and `source_id` so re-running the backfill is idempotent.

Revision ID: a7b8c9d0e1f2
Revises: a7c3e9f1b2d4
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'a7c3e9f1b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('areas', 'photo_url', new_column_name='default_photo_url')

    op.create_table(
        'area_photos',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('area_id', sa.Uuid(), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('source', sa.String(length=60), nullable=False),
        sa.Column('source_id', sa.String(length=255), nullable=True),
        sa.Column('attribution', sa.String(length=500), nullable=True),
        sa.Column('attribution_url', sa.String(length=2048), nullable=True),
        sa.Column('position', sa.Integer(), server_default='0', nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('color', sa.String(length=16), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['area_id'], ['areas.id'],
            name=op.f('fk_area_photos_area_id_areas'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_area_photos')),
        sa.UniqueConstraint(
            'area_id', 'source', 'source_id', name='uq_area_photos_area_source_id'
        ),
    )
    op.create_index(op.f('ix_area_photos_area_id'), 'area_photos', ['area_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_area_photos_area_id'), table_name='area_photos')
    op.drop_table('area_photos')
    op.alter_column('areas', 'default_photo_url', new_column_name='photo_url')
