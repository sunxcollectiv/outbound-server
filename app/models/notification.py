from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.user import User


class NotificationCategory(StrEnum):
    """The coarse kind of a notification: what a user can switch on or off per
    channel (see app.services.notifications.DEFAULTS). The finer `type` on a
    notification says what exactly happened."""

    ANNOUNCEMENT = "announcement"  # service notices from us, sent as broadcasts
    PROMOTION = "promotion"  # marketing, sent as broadcasts; opt-in off the inbox
    TRIP_ACTIVITY = "trip_activity"  # something another member did on a shared trip
    TRAVEL_ALERT = "travel_alert"  # about the user's own travel: flight details, reminders


class NotificationChannel(StrEnum):
    IN_APP = "in_app"  # the inbox (notifications table)
    PUSH = "push"  # to push_devices: APNs (app.services.push); no Android sender yet
    EMAIL = "email"  # no sender yet


class PushPlatform(StrEnum):
    IOS = "ios"
    ANDROID = "android"
    WEB = "web"


class Notification(UUIDPrimaryKeyMixin, Base):
    """One entry in one user's inbox. Rows are per recipient, so read state is
    just columns here, and the inbox is a single indexed query.

    The text is a snapshot written at creation (title/body), alongside `type`
    and `data` for the client to render and deep-link from. The trip/item links
    are SET NULL so the snapshot outlives what it was about.

    Most rows are written by app.services.notifications.notify. Broadcasts live
    in `announcements` and are copied in per user the first time that user's
    inbox is read (sync_announcements), so people who sign up while one is
    running still get it.

    `visible_at` lets a notification be written ahead of time ("your flight is
    tomorrow") and stay out of the inbox until it's due; `expires_at` takes it
    out again. `archived_at` is a dismissal, kept as a row so a dismissed
    announcement isn't copied in again.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Postgres treats NULLs as distinct, so both only bind when set.
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_id_dedupe_key"),
        UniqueConstraint(
            "user_id", "announcement_id", name="uq_notifications_user_id_announcement_id"
        ),
        Index(
            "ix_notifications_user_id_visible_at",
            "user_id",
            text("visible_at DESC"),
            text("id DESC"),
        ),
        # The badge: unread, undismissed rows per user.
        Index(
            "ix_notifications_user_id_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL AND archived_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category: Mapped[NotificationCategory] = mapped_column(
        str_enum(NotificationCategory, "notification_category")
    )
    # Free-form and dotted, e.g. "trip_item.updated" or "flight.reminder".
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    # Ids and extras for the client to render and deep-link with.
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Who caused it, when it's someone's doing.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL"), index=True
    )
    trip_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trip_items.id", ondelete="SET NULL"), index=True
    )
    # Set on the copies of an announcement; deleting it clears every inbox.
    announcement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), index=True
    )
    # Writing again with the same key updates this row instead of adding one,
    # and marks it unread again: retries are idempotent and repeats collapse.
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    visible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    actor: Mapped[User | None] = relationship(foreign_keys=[actor_id])


class Announcement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A broadcast to every user, live between starts_at and ends_at. Created by
    admins (/announcements). Users get a copy in `notifications` the first
    time their inbox is read while it runs; edits are copied onto those rows.

    Everyone is the only audience for now; targeting (by country, platform...)
    would be a column here."""

    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint(
            "category IN ('announcement', 'promotion')", name="announcement_broadcast_category"
        ),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="announcement_window"),
    )

    category: Mapped[NotificationCategory] = mapped_column(
        str_enum(NotificationCategory, "notification_category")
    )
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class NotificationPreference(Base):
    """A user's override of the default for one category on one channel. Only
    changes are stored; the defaults live in app.services.notifications."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[NotificationCategory] = mapped_column(
        str_enum(NotificationCategory, "notification_category"), primary_key=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        str_enum(NotificationChannel, "notification_channel"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PushDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A device the app can push to: an APNs/FCM/web-push token registered by
    the client after login. A token belongs to one user at a time; registering
    it again from another account moves it. iOS tokens get pushes via
    app.services.push, which deletes any Apple reports as dead."""

    __tablename__ = "push_devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[PushPlatform] = mapped_column(str_enum(PushPlatform, "push_platform"))
    token: Mapped[str] = mapped_column(String(4096), unique=True)
    app_version: Mapped[str | None] = mapped_column(String(40))
    locale: Mapped[str | None] = mapped_column(String(35))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Bumped on every registration; stale tokens can be pruned off this.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
