"""split places and areas

Repurpose the model: the old `locations` catalogue of specific spots becomes
`places`, and a new, slim `areas` table holds the place-on-the-map a
place sits in. Trip items now reference places. Done with renames so no rows
are dropped.

Revision ID: 71661082a7a8
Revises: 221dd0d93d75
Create Date: 2026-10-14 14:04:16.613564

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '71661082a7a8'
down_revision: Union[str, None] = '221dd0d93d75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The old locations catalogue of specific spots becomes `places`.
    op.rename_table('locations', 'places')
    op.execute('ALTER TABLE places RENAME CONSTRAINT pk_locations TO pk_places')
    op.execute(
        'ALTER TABLE places RENAME CONSTRAINT uq_locations_google_place_id '
        'TO uq_places_google_place_id'
    )
    op.execute(
        'ALTER TABLE places RENAME CONSTRAINT fk_locations_created_by_id_users '
        'TO fk_places_created_by_id_users'
    )
    op.execute('ALTER INDEX ix_locations_name RENAME TO ix_places_name')

    # Trip items now point at places rather than the catalogue's old name.
    op.drop_constraint('fk_trip_items_location_id_locations', 'trip_items', type_='foreignkey')
    op.drop_constraint('fk_trip_items_end_location_id_locations', 'trip_items', type_='foreignkey')
    op.alter_column('trip_items', 'location_id', new_column_name='place_id')
    op.alter_column('trip_items', 'end_location_id', new_column_name='end_place_id')
    op.execute('ALTER INDEX ix_trip_items_location_id RENAME TO ix_trip_items_place_id')
    op.create_foreign_key(
        op.f('fk_trip_items_place_id_places'),
        'trip_items', 'places', ['place_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_trip_items_end_place_id_places'),
        'trip_items', 'places', ['end_place_id'], ['id'], ondelete='SET NULL',
    )

    # A new, slim `areas` table: the place-on-the-map a place sits in.
    op.create_table(
        'areas',
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('photo_url', sa.String(length=2048), nullable=True),
        sa.Column('created_by_id', sa.Uuid(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(
            ['created_by_id'], ['users.id'],
            name=op.f('fk_areas_created_by_id_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_areas')),
    )
    op.create_index(op.f('ix_areas_name'), 'areas', ['name'], unique=False)

    # Places link to their area.
    op.add_column('places', sa.Column('area_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_places_area_id'), 'places', ['area_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_places_area_id_areas'),
        'places', 'areas', ['area_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(op.f('fk_places_area_id_areas'), 'places', type_='foreignkey')
    op.drop_index(op.f('ix_places_area_id'), table_name='places')
    op.drop_column('places', 'area_id')

    op.drop_index(op.f('ix_areas_name'), table_name='areas')
    op.drop_table('areas')

    op.drop_constraint(op.f('fk_trip_items_end_place_id_places'), 'trip_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_trip_items_place_id_places'), 'trip_items', type_='foreignkey')
    op.execute('ALTER INDEX ix_trip_items_place_id RENAME TO ix_trip_items_location_id')
    op.alter_column('trip_items', 'place_id', new_column_name='location_id')
    op.alter_column('trip_items', 'end_place_id', new_column_name='end_location_id')

    op.execute('ALTER INDEX ix_places_name RENAME TO ix_locations_name')
    op.execute(
        'ALTER TABLE places RENAME CONSTRAINT fk_places_created_by_id_users '
        'TO fk_locations_created_by_id_users'
    )
    op.execute(
        'ALTER TABLE places RENAME CONSTRAINT uq_places_google_place_id '
        'TO uq_locations_google_place_id'
    )
    op.execute('ALTER TABLE places RENAME CONSTRAINT pk_places TO pk_locations')
    op.rename_table('places', 'locations')

    op.create_foreign_key(
        'fk_trip_items_location_id_locations',
        'trip_items', 'locations', ['location_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_trip_items_end_location_id_locations',
        'trip_items', 'locations', ['end_location_id'], ['id'], ondelete='SET NULL',
    )
