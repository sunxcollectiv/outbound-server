"""add default_photo attribution columns to areas

Denormalize the cover photo's attribution onto the area row (next to
default_photo_url) so an ordinary read — a trip with its areas, a search — can
show the cover with its required Unsplash credit without ever loading
area_photos. area_photos stays a candidate pool for a later admin UI.

Revision ID: b2c3d4e5f6a7
Revises: a7b8c9d0e1f2
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('areas', sa.Column('default_photo_attribution', sa.String(length=500), nullable=True))
    op.add_column('areas', sa.Column('default_photo_attribution_url', sa.String(length=2048), nullable=True))


def downgrade() -> None:
    op.drop_column('areas', 'default_photo_attribution_url')
    op.drop_column('areas', 'default_photo_attribution')
