"""Notifications: the one way into a user's inbox, and how it's read back.

Feature code calls `notify` (with `trip_audience` to pick who hears about a
trip change); none of these functions commit, so a notification lands in the
same transaction as the change it reports and vanishes with it on rollback.

What reaches a user is gated by their preferences: a category × channel matrix
whose defaults live in DEFAULTS, with only a user's changes stored. The inbox
is the in_app channel. Push goes out from `notify` once the caller commits
(app.services.push); email has preferences but no sender yet.

Broadcasts are `Announcement` rows, copied into a user's inbox by
`sync_announcements` the first time it's read while they run, so the inbox is
one table with one notion of read/unread.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, Uuid, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Announcement,
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPreference,
    Trip,
    TripItem,
    TripItemParticipant,
    TripItemVisibility,
    TripMember,
    User,
)
from app.services import push

C = NotificationCategory
CH = NotificationChannel

# What each category does on each channel for someone who hasn't said
# otherwise. Marketing stays opt-in off the inbox.
DEFAULTS: dict[NotificationCategory, dict[NotificationChannel, bool]] = {
    C.ANNOUNCEMENT: {CH.IN_APP: True, CH.PUSH: True, CH.EMAIL: False},
    C.PROMOTION: {CH.IN_APP: True, CH.PUSH: False, CH.EMAIL: False},
    C.TRIP_ACTIVITY: {CH.IN_APP: True, CH.PUSH: True, CH.EMAIL: False},
    C.TRAVEL_ALERT: {CH.IN_APP: True, CH.PUSH: True, CH.EMAIL: False},
}

# Can't be switched off: service notices always reach the inbox.
LOCKED: frozenset[tuple[NotificationCategory, NotificationChannel]] = frozenset(
    {(C.ANNOUNCEMENT, CH.IN_APP)}
)


# --- preferences ---


def preference_matrix(session: Session, user: User) -> dict[tuple[C, CH], bool]:
    """Every category × channel for `user`: their stored choice, else the default."""
    stored = {
        (pref.category, pref.channel): pref.enabled
        for pref in session.scalars(
            select(NotificationPreference).where(NotificationPreference.user_id == user.id)
        )
    }
    return {
        (category, channel): default if (category, channel) in LOCKED
        else stored.get((category, channel), default)
        for category, channels in DEFAULTS.items()
        for channel, default in channels.items()
    }


def enabled(
    session: Session,
    user_ids: Iterable[uuid.UUID],
    category: NotificationCategory,
    channel: NotificationChannel,
) -> set[uuid.UUID]:
    """Those of `user_ids` who get `category` on `channel`."""
    user_ids = set(user_ids)
    if (category, channel) in LOCKED or not user_ids:
        return user_ids
    overrides = {
        user_id: on
        for user_id, on in session.execute(
            select(NotificationPreference.user_id, NotificationPreference.enabled).where(
                NotificationPreference.user_id.in_(user_ids),
                NotificationPreference.category == category,
                NotificationPreference.channel == channel,
            )
        )
    }
    default = DEFAULTS[category][channel]
    return {user_id for user_id in user_ids if overrides.get(user_id, default)}


def _muted_in_app(session: Session, user: User) -> list[NotificationCategory]:
    return [
        category
        for (category, channel), on in preference_matrix(session, user).items()
        if channel is CH.IN_APP and not on
    ]


# --- writing ---


def notify(
    session: Session,
    *,
    user_ids: Iterable[uuid.UUID],
    category: NotificationCategory,
    type: str,
    title: str,
    body: str | None = None,
    data: dict[str, Any] | None = None,
    actor_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    trip_item_id: uuid.UUID | None = None,
    visible_at: datetime | None = None,
    expires_at: datetime | None = None,
    dedupe_key: str | None = None,
) -> int:
    """Put a notification in each recipient's inbox; returns how many got one.

    The actor never hears about their own doing, and anyone who has switched
    `category` off in the inbox is skipped. Once the session commits, those
    with push on for `category` get it on their phones too (unless it's
    scheduled: those aren't pushed yet). `visible_at` (aware) holds it back
    until then, e.g. a reminder written when the flight is booked;
    `expires_at` takes it out of the inbox again.

    With a `dedupe_key`, a user who already has a notification under that key
    gets it rewritten instead of a second one, unread and undismissed again:
    retrying is safe, and repeated news ("flight time changed") collapses into
    one entry. Keys are per user, so name them for what they're about, e.g.
    f"flight-reminder:{item_id}".
    """
    recipients = set(user_ids)
    recipients.discard(actor_id)
    recipients = enabled(session, recipients, category, CH.IN_APP)
    if not recipients:
        return 0

    content = {
        "category": category,
        "type": type,
        "title": title,
        "body": body,
        "data": data or {},
        "actor_id": actor_id,
        "trip_id": trip_id,
        "trip_item_id": trip_item_id,
        "visible_at": visible_at if visible_at is not None else func.now(),
        "expires_at": expires_at,
        "dedupe_key": dedupe_key,
    }
    statement = insert(Notification).values(
        [{"id": uuid.uuid4(), "user_id": user_id, **content} for user_id in sorted(recipients)]
    )
    if dedupe_key is not None:
        refreshed = {
            field: statement.excluded[field] for field in content if field != "dedupe_key"
        }
        statement = statement.on_conflict_do_update(
            constraint="uq_notifications_user_id_dedupe_key",
            set_={**refreshed, "read_at": None, "archived_at": None},
        )
    # On a dedupe conflict the existing row's id comes back, not the new one.
    ids = list(session.scalars(statement.returning(Notification.id)))
    if visible_at is None or visible_at <= datetime.now(UTC):
        push.queue(session, ids)
    # Email goes out from here once it has a sender: filter the same
    # recipients through enabled(..., CH.EMAIL).
    return len(recipients)


def trip_audience(session: Session, trip: Trip, item: TripItem | None = None) -> list[uuid.UUID]:
    """The accounts on `trip` who may hear about it, or about `item` on it.

    Members without an account have no inbox and are left out. For an item
    this follows app.services.item_visibility: a private item reaches its
    participants only. A closed item reaches everyone, but non-participants may
    only see its kind, title and times, so keep anything else out of what you
    write to them (or notify participants and the rest separately).

    Pass the result to `notify`, which drops the actor."""
    query = select(TripMember.user_id).where(
        TripMember.trip_id == trip.id, TripMember.user_id.is_not(None)
    )
    if item is not None and item.visibility is TripItemVisibility.PRIVATE:
        query = query.where(
            TripMember.id.in_(
                select(TripItemParticipant.member_id).where(TripItemParticipant.item_id == item.id)
            )
        )
    return list(session.scalars(query))


# --- announcements ---


def _running() -> ColumnElement[bool]:
    now = func.now()
    return (Announcement.starts_at <= now) & or_(
        Announcement.ends_at.is_(None), Announcement.ends_at > now
    )


def sync_announcements(session: Session, user: User) -> None:
    """Copy announcements running now that `user` doesn't have yet into their
    inbox. Call before reading the inbox; cheap when there's nothing new. A
    dismissed copy stays, so a dismissal sticks."""
    muted = _muted_in_app(session, user)
    already = exists().where(
        Notification.user_id == user.id, Notification.announcement_id == Announcement.id
    )
    source = select(
        func.gen_random_uuid(),
        literal(user.id, Uuid),
        Announcement.id,
        Announcement.category,
        Announcement.type,
        Announcement.title,
        Announcement.body,
        Announcement.data,
        Announcement.starts_at,
        Announcement.ends_at,
    ).where(_running(), ~already)
    if muted:
        source = source.where(Announcement.category.not_in(muted))
    session.execute(
        insert(Notification)
        .from_select(
            [
                "id",
                "user_id",
                "announcement_id",
                "category",
                "type",
                "title",
                "body",
                "data",
                "visible_at",
                "expires_at",
            ],
            source,
        )
        .on_conflict_do_nothing(constraint="uq_notifications_user_id_announcement_id")
    )


def propagate_announcement(session: Session, announcement: Announcement) -> None:
    """Carry an edited announcement onto the copies already in inboxes."""
    session.execute(
        update(Notification)
        .where(Notification.announcement_id == announcement.id)
        .values(
            category=announcement.category,
            type=announcement.type,
            title=announcement.title,
            body=announcement.body,
            data=announcement.data,
            visible_at=announcement.starts_at,
            expires_at=announcement.ends_at,
        )
    )


# --- reading ---


def in_inbox(session: Session, user: User) -> list[ColumnElement[bool]]:
    """What makes a row show in `user`'s inbox right now: theirs, due, not
    expired or dismissed, and in a category they haven't switched off."""
    now = func.now()
    conditions = [
        Notification.user_id == user.id,
        Notification.archived_at.is_(None),
        Notification.visible_at <= now,
        or_(Notification.expires_at.is_(None), Notification.expires_at > now),
    ]
    muted = _muted_in_app(session, user)
    if muted:
        conditions.append(Notification.category.not_in(muted))
    return conditions


def inbox_query(
    session: Session,
    user: User,
    *,
    unread_only: bool = False,
    category: NotificationCategory | None = None,
) -> Select[tuple[Notification]]:
    """`user`'s inbox, newest first."""
    query = (
        select(Notification)
        .where(*in_inbox(session, user))
        .options(selectinload(Notification.actor))
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if category is not None:
        query = query.where(Notification.category == category)
    return query


def unread_count(session: Session, user: User) -> int:
    return session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(*in_inbox(session, user), Notification.read_at.is_(None))
    ) or 0
