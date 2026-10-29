"""trip items: title optional when the item has a place

trip_items.title becomes nullable: an item can go by its place's name instead.
The API requires a title or a place; the database doesn't, since deleting a
place nulls items' place_id.

Downgrade names untitled items after their place (else their kind) before
restoring NOT NULL, so no rows are lost.

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8b9c0d1e2f3a'
down_revision: Union[str, None] = '7a8b9c0d1e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('trip_items', 'title', existing_type=sa.String(length=200), nullable=True)


def downgrade() -> None:
    op.execute(
        """
        UPDATE trip_items SET title = COALESCE(
            (SELECT NULLIF(places.name, '') FROM places WHERE places.id = trip_items.place_id),
            initcap(trip_items.kind)
        )
        WHERE title IS NULL
        """
    )
    op.alter_column('trip_items', 'title', existing_type=sa.String(length=200), nullable=False)
