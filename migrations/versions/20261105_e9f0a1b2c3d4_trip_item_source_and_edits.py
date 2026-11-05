"""trip item provenance: source, receipt link, edited_at/edited_by

- source: manual | email | image (server default manual), set at creation.
- source_email_receipt_id / source_image_id: the receipt an imported item was
  read from (ON DELETE SET NULL), at most one and of the item's own source type.
- edited_at / edited_by_id: the last time a person changed the booking itself.

Existing imports are linked back to their receipt where the match is
unambiguous: a processed receipt's extracted item with the same kind, in the
receipt's trip, created by the receipt's owner, and either the same
confirmation number or (neither having one) the same title — and no other
receipt matching the item. Everything else stays `manual`. edited_at is left
null (unknown) for existing rows.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9f0a1b2c3d4'
down_revision: Union[str, None] = 'd8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SOURCES = ('manual', 'email', 'image')

_LINK_EXISTING = """
WITH candidates AS (
    SELECT i.id AS item_id, r.id AS receipt_id
    FROM {table} r
    JOIN trip_items i ON i.trip_id = r.trip_id AND i.created_by_id = r.user_id
    CROSS JOIN LATERAL jsonb_array_elements(r.extracted -> 'items') AS x
    WHERE r.status = 'processed'
      AND x ->> 'kind' = i.kind
      AND (
          (i.confirmation_number IS NOT NULL
           AND i.confirmation_number = btrim(x ->> 'confirmation_number'))
          OR (i.confirmation_number IS NULL
              AND NULLIF(btrim(x ->> 'confirmation_number'), '') IS NULL
              AND i.title = left(x ->> 'title', 200))
      )
), unique_matches AS (
    SELECT item_id, (array_agg(receipt_id))[1] AS receipt_id
    FROM candidates
    GROUP BY item_id
    HAVING count(DISTINCT receipt_id) = 1
)
UPDATE trip_items i
SET source = '{source}', {column} = m.receipt_id
FROM unique_matches m
WHERE i.id = m.item_id AND i.source = 'manual'
"""


def upgrade() -> None:
    op.add_column(
        'trip_items',
        sa.Column(
            'source',
            sa.Enum(*_SOURCES, name='trip_item_source', native_enum=False, length=32),
            server_default='manual',
            nullable=False,
        ),
    )
    op.add_column('trip_items', sa.Column('source_email_receipt_id', sa.Uuid(), nullable=True))
    op.add_column('trip_items', sa.Column('source_image_id', sa.Uuid(), nullable=True))
    op.add_column('trip_items', sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trip_items', sa.Column('edited_by_id', sa.Uuid(), nullable=True))

    # Bare tokens: the naming convention renders ck_<table>_<token>.
    op.create_check_constraint(
        'trip_item_source', 'trip_items', "source IN ('manual', 'email', 'image')"
    )
    op.create_check_constraint(
        'source_link_matches', 'trip_items',
        "(source_email_receipt_id IS NULL OR source = 'email')"
        " AND (source_image_id IS NULL OR source = 'image')",
    )
    op.create_foreign_key(
        op.f('fk_trip_items_source_email_receipt_id_r_email_receipts'),
        'trip_items', 'r_email_receipts', ['source_email_receipt_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_trip_items_source_image_id_r_images'),
        'trip_items', 'r_images', ['source_image_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_trip_items_edited_by_id_users'),
        'trip_items', 'users', ['edited_by_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index(
        op.f('ix_trip_items_source_email_receipt_id'), 'trip_items', ['source_email_receipt_id']
    )
    op.create_index(op.f('ix_trip_items_source_image_id'), 'trip_items', ['source_image_id'])

    op.execute(_LINK_EXISTING.format(
        table='r_email_receipts', source='email', column='source_email_receipt_id'
    ))
    op.execute(_LINK_EXISTING.format(table='r_images', source='image', column='source_image_id'))


def downgrade() -> None:
    op.drop_index(op.f('ix_trip_items_source_image_id'), table_name='trip_items')
    op.drop_index(op.f('ix_trip_items_source_email_receipt_id'), table_name='trip_items')
    op.drop_constraint(op.f('fk_trip_items_edited_by_id_users'), 'trip_items', type_='foreignkey')
    op.drop_constraint(
        op.f('fk_trip_items_source_image_id_r_images'), 'trip_items', type_='foreignkey'
    )
    op.drop_constraint(
        op.f('fk_trip_items_source_email_receipt_id_r_email_receipts'),
        'trip_items', type_='foreignkey',
    )
    op.drop_constraint('source_link_matches', 'trip_items', type_='check')
    op.drop_constraint('trip_item_source', 'trip_items', type_='check')
    op.drop_column('trip_items', 'edited_by_id')
    op.drop_column('trip_items', 'edited_at')
    op.drop_column('trip_items', 'source_image_id')
    op.drop_column('trip_items', 'source_email_receipt_id')
    op.drop_column('trip_items', 'source')
