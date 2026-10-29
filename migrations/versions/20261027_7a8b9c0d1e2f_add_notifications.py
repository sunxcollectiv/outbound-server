"""notifications: inbox, announcements, preferences, push devices, admins

The groundwork for notifications. `notifications` is the per-user inbox (read
state lives on the row); `announcements` are broadcasts, copied into each
user's inbox the first time it's read while they run; `notification_preferences`
holds users' overrides of the per-category, per-channel defaults; `push_devices`
holds client push tokens (nothing sends to them yet). users.is_admin gates the
announcement endpoints.

Only adds: nothing existing is touched.

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7a8b9c0d1e2f'
down_revision: Union[str, None] = '6f7a8b9c0d1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CATEGORIES = ('announcement', 'promotion', 'trip_activity', 'travel_alert')
_CATEGORY_CHECK = "category IN ('announcement', 'promotion', 'trip_activity', 'travel_alert')"


def _category() -> sa.Enum:
    return sa.Enum(*_CATEGORIES, name='notification_category', native_enum=False, length=32)


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )

    op.create_table(
        'announcements',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('category', _category(), nullable=False),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(_CATEGORY_CHECK, name=op.f('ck_announcements_notification_category')),
        sa.CheckConstraint(
            "category IN ('announcement', 'promotion')",
            name=op.f('ck_announcements_announcement_broadcast_category'),
        ),
        sa.CheckConstraint(
            'ends_at IS NULL OR ends_at > starts_at',
            name=op.f('ck_announcements_announcement_window'),
        ),
        sa.ForeignKeyConstraint(
            ['created_by_id'], ['users.id'],
            name=op.f('fk_announcements_created_by_id_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_announcements')),
    )

    op.create_table(
        'notifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('category', _category(), nullable=False),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('trip_id', sa.Uuid(), nullable=True),
        sa.Column('trip_item_id', sa.Uuid(), nullable=True),
        sa.Column('announcement_id', sa.Uuid(), nullable=True),
        sa.Column('dedupe_key', sa.String(length=200), nullable=True),
        sa.Column('visible_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(_CATEGORY_CHECK, name=op.f('ck_notifications_notification_category')),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_notifications_user_id_users'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['actor_id'], ['users.id'],
            name=op.f('fk_notifications_actor_id_users'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['trip_id'], ['trips.id'],
            name=op.f('fk_notifications_trip_id_trips'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['trip_item_id'], ['trip_items.id'],
            name=op.f('fk_notifications_trip_item_id_trip_items'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['announcement_id'], ['announcements.id'],
            name=op.f('fk_notifications_announcement_id_announcements'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
        sa.UniqueConstraint('user_id', 'dedupe_key', name='uq_notifications_user_id_dedupe_key'),
        sa.UniqueConstraint(
            'user_id', 'announcement_id', name='uq_notifications_user_id_announcement_id'
        ),
    )
    op.create_index(
        'ix_notifications_user_id_visible_at',
        'notifications',
        ['user_id', sa.literal_column('visible_at DESC'), sa.literal_column('id DESC')],
    )
    op.create_index(
        'ix_notifications_user_id_unread',
        'notifications',
        ['user_id'],
        postgresql_where=sa.text('read_at IS NULL AND archived_at IS NULL'),
    )
    op.create_index(op.f('ix_notifications_trip_id'), 'notifications', ['trip_id'])
    op.create_index(op.f('ix_notifications_trip_item_id'), 'notifications', ['trip_item_id'])
    op.create_index(op.f('ix_notifications_announcement_id'), 'notifications', ['announcement_id'])

    op.create_table(
        'notification_preferences',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('category', _category(), nullable=False),
        sa.Column(
            'channel',
            sa.Enum('in_app', 'push', 'email', name='notification_channel', native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            _CATEGORY_CHECK, name=op.f('ck_notification_preferences_notification_category')
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'push', 'email')",
            name=op.f('ck_notification_preferences_notification_channel'),
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_notification_preferences_user_id_users'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'user_id', 'category', 'channel', name=op.f('pk_notification_preferences')
        ),
    )

    op.create_table(
        'push_devices',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'platform',
            sa.Enum('ios', 'android', 'web', name='push_platform', native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column('token', sa.String(length=4096), nullable=False),
        sa.Column('app_version', sa.String(length=40), nullable=True),
        sa.Column('locale', sa.String(length=35), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "platform IN ('ios', 'android', 'web')", name=op.f('ck_push_devices_push_platform')
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_push_devices_user_id_users'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_push_devices')),
        sa.UniqueConstraint('token', name=op.f('uq_push_devices_token')),
    )
    op.create_index(op.f('ix_push_devices_user_id'), 'push_devices', ['user_id'])


def downgrade() -> None:
    op.drop_table('push_devices')
    op.drop_table('notification_preferences')
    op.drop_table('notifications')
    op.drop_table('announcements')
    op.drop_column('users', 'is_admin')
