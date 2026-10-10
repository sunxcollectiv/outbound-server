"""initial schema

Revision ID: 7c46bb2a5f44
Revises: 
Create Date: 2026-10-09 19:07:30.628418

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7c46bb2a5f44'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=False),
    sa.Column('avatar_url', sa.String(length=2048), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('locations',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=60), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('photo_url', sa.String(length=2048), nullable=True),
    sa.Column('address', sa.String(length=500), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('google_place_id', sa.String(length=255), nullable=True),
    sa.Column('google_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('google_fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_locations_created_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_locations')),
    sa.UniqueConstraint('google_place_id', name=op.f('uq_locations_google_place_id'))
    )
    op.create_index(op.f('ix_locations_name'), 'locations', ['name'], unique=False)
    op.create_table('trips',
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('cover_image_url', sa.String(length=2048), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('end_date >= start_date', name=op.f('ck_trips_dates_ordered')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_trips_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trips'))
    )
    op.create_index(op.f('ix_trips_owner_id'), 'trips', ['owner_id'], unique=False)
    op.create_table('trip_items',
    sa.Column('trip_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('lodging', 'activity', 'transit', name='trip_item_kind', native_enum=False, length=32), nullable=False),
    sa.Column('scope', sa.Enum('group', 'personal', name='trip_item_scope', native_enum=False, length=32), nullable=False),
    sa.Column('requirement', sa.Enum('mandatory', 'recommended', 'optional', name='trip_item_requirement', native_enum=False, length=32), nullable=True),
    sa.Column('visibility', sa.Enum('private', 'shared', 'open', name='trip_item_visibility', native_enum=False, length=32), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.String(length=60)), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('image_url', sa.String(length=2048), nullable=True),
    sa.Column('location_id', sa.Uuid(), nullable=True),
    sa.Column('end_location_id', sa.Uuid(), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(scope = 'group') = (requirement IS NOT NULL)", name=op.f('ck_trip_items_group_has_requirement')),
    sa.CheckConstraint("(scope = 'personal') = (visibility IS NOT NULL)", name=op.f('ck_trip_items_personal_has_visibility')),
    sa.CheckConstraint("kind IN ('lodging', 'activity', 'transit')", name=op.f('ck_trip_items_trip_item_kind')),
    sa.CheckConstraint("requirement IN ('mandatory', 'recommended', 'optional')", name=op.f('ck_trip_items_trip_item_requirement')),
    sa.CheckConstraint("scope IN ('group', 'personal')", name=op.f('ck_trip_items_trip_item_scope')),
    sa.CheckConstraint("visibility IN ('private', 'shared', 'open')", name=op.f('ck_trip_items_trip_item_visibility')),
    sa.CheckConstraint('ends_at IS NULL OR ends_at >= starts_at', name=op.f('ck_trip_items_times_ordered')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_trip_items_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['end_location_id'], ['locations.id'], name=op.f('fk_trip_items_end_location_id_locations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['location_id'], ['locations.id'], name=op.f('fk_trip_items_location_id_locations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], name=op.f('fk_trip_items_trip_id_trips'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trip_items'))
    )
    op.create_index(op.f('ix_trip_items_location_id'), 'trip_items', ['location_id'], unique=False)
    op.create_index('ix_trip_items_trip_id_starts_at', 'trip_items', ['trip_id', 'starts_at'], unique=False)
    op.create_table('trip_members',
    sa.Column('trip_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('organizer', 'member', name='trip_role', native_enum=False, length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('organizer', 'member')", name=op.f('ck_trip_members_trip_role')),
    sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], name=op.f('fk_trip_members_trip_id_trips'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_trip_members_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('trip_id', 'user_id', name=op.f('pk_trip_members'))
    )
    op.create_index(op.f('ix_trip_members_user_id'), 'trip_members', ['user_id'], unique=False)
    op.create_table('trip_item_participants',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Enum('going', 'maybe', 'declined', name='rsvp_status', native_enum=False, length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('going', 'maybe', 'declined')", name=op.f('ck_trip_item_participants_rsvp_status')),
    sa.ForeignKeyConstraint(['item_id'], ['trip_items.id'], name=op.f('fk_trip_item_participants_item_id_trip_items'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_trip_item_participants_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('item_id', 'user_id', name=op.f('pk_trip_item_participants'))
    )
    op.create_index(op.f('ix_trip_item_participants_user_id'), 'trip_item_participants', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_trip_item_participants_user_id'), table_name='trip_item_participants')
    op.drop_table('trip_item_participants')
    op.drop_index(op.f('ix_trip_members_user_id'), table_name='trip_members')
    op.drop_table('trip_members')
    op.drop_index('ix_trip_items_trip_id_starts_at', table_name='trip_items')
    op.drop_index(op.f('ix_trip_items_location_id'), table_name='trip_items')
    op.drop_table('trip_items')
    op.drop_index(op.f('ix_trips_owner_id'), table_name='trips')
    op.drop_table('trips')
    op.drop_index(op.f('ix_locations_name'), table_name='locations')
    op.drop_table('locations')
    op.drop_table('users')
