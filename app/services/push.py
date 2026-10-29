"""Push notifications: getting a new inbox row onto the user's phone.

`notify` queues the ids of the rows it wrote on the session (`queue`). Nothing
goes out until that session commits: an after_commit hook hands the ids to the
dispatcher, which delivers them on a background thread with its own session,
so the request never waits on Apple. A rollback drops the queue, and delivery
re-reads each row by id anyway, so nothing is ever pushed for a row that
didn't land.

Delivery (`deliver`) sends each row that's due to the user's iOS devices if
they have push on for its category, with their unread count as the badge, and
forgets device tokens Apple says are dead. Failures are logged, never raised:
the inbox row is the record, a push is only a nudge.

Only iOS for now. Scheduled rows (a future visible_at) and announcements aren't
pushed: they need a scheduler and a fan-out job.
"""

import logging
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session, SessionTransaction

from app.db import SessionLocal
from app.models import (
    Notification,
    NotificationChannel,
    PushDevice,
    PushPlatform,
    Trip,
    User,
)
from app.services.apns import ApnsClient, get_apns_client

logger = logging.getLogger(__name__)

_QUEUE_KEY = "pending_push_notification_ids"

# Keeps the payload well under APNs' 4 KB; the full text is in the inbox.
BODY_MAX_CHARS = 1000


def queue(session: Session, notification_ids: Iterable[uuid.UUID]) -> None:
    """Push these notifications once `session` commits."""
    session.info.setdefault(_QUEUE_KEY, []).extend(notification_ids)


@event.listens_for(Session, "after_commit")
def _send_queued(session: Session) -> None:
    ids = session.info.pop(_QUEUE_KEY, None)
    if ids:
        dispatcher(ids)


@event.listens_for(Session, "after_soft_rollback")
def _drop_queued(session: Session, previous_transaction: SessionTransaction) -> None:
    # Only a rollback of the whole transaction. A savepoint rolling back keeps
    # the queue; delivery skips the ids of rows that went with it.
    if previous_transaction.parent is None:
        session.info.pop(_QUEUE_KEY, None)


# --- dispatch ---


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push")


def _deliver_in_background(notification_ids: list[uuid.UUID]) -> None:
    client = get_apns_client()
    if client is None:
        return  # push isn't configured

    def run() -> None:
        try:
            with SessionLocal() as session:
                deliver(session, notification_ids, client)
        except Exception:
            logger.exception("push delivery failed for %s", notification_ids)

    _executor.submit(run)


# Called with the ids a commit queued. Tests swap it for a recorder.
dispatcher: Callable[[list[uuid.UUID]], None] = _deliver_in_background


# --- delivery ---


# The iOS notification category for pushes about a trip: the app registers a
# content extension (the rich card) and its actions under this name.
TRIP_CATEGORY = "TRIP_ACTIVITY"


def payload(
    notification: Notification, badge: int, trip_title: str | None = None
) -> dict[str, Any]:
    """What the app gets: the alert, the badge, and the ids it opens on tap.
    A push about a trip also names the iOS category for the rich card, with
    the trip's current name for it. Keys with nothing to say are left out."""
    alert = {"title": notification.title}
    if notification.body:
        alert["body"] = notification.body[:BODY_MAX_CHARS]
    aps: dict[str, Any] = {"alert": alert, "sound": "default", "badge": badge}
    content: dict[str, Any] = {"aps": aps, "notification_id": str(notification.id)}
    if notification.trip_id is not None:
        aps["category"] = TRIP_CATEGORY
        content["trip_id"] = str(notification.trip_id)
        if trip_title is not None:
            content["trip_title"] = trip_title
    if notification.trip_item_id is not None:
        content["trip_item_id"] = str(notification.trip_item_id)
    return content


def deliver(session: Session, notification_ids: Iterable[uuid.UUID], client: ApnsClient) -> int:
    """Push each of these notifications to its owner's iOS devices; returns
    how many pushes Apple accepted. Commits any pruned device tokens."""
    # Imported here: notifications imports this module to queue.
    from app.services.notifications import enabled, unread_count

    notifications = list(
        session.scalars(
            select(Notification).where(
                Notification.id.in_(list(notification_ids)),
                Notification.visible_at <= func.now(),
                Notification.archived_at.is_(None),
                Notification.announcement_id.is_(None),
            )
        )
    )
    push_on = {
        category: enabled(
            session,
            {n.user_id for n in notifications if n.category == category},
            category,
            NotificationChannel.PUSH,
        )
        for category in {n.category for n in notifications}
    }
    wanted = [n for n in notifications if n.user_id in push_on[n.category]]
    if not wanted:
        return 0

    owners = {n.user_id for n in wanted}
    devices: dict[uuid.UUID, list[PushDevice]] = {}
    for device in session.scalars(
        select(PushDevice).where(
            PushDevice.user_id.in_(owners), PushDevice.platform == PushPlatform.IOS
        )
    ):
        devices.setdefault(device.user_id, []).append(device)
    badges = {
        user.id: unread_count(session, user)
        for user in session.scalars(select(User).where(User.id.in_(owners)))
    }
    # The trip's name as it is now, for the rich card's tag.
    trip_titles: dict[uuid.UUID, str] = {}
    trip_ids = {n.trip_id for n in wanted if n.trip_id is not None}
    if trip_ids:
        rows = session.execute(select(Trip.id, Trip.title).where(Trip.id.in_(trip_ids)))
        trip_titles = {trip_id: title for trip_id, title in rows}

    accepted = 0
    dead: set[uuid.UUID] = set()
    for notification in wanted:
        content = payload(
            notification, badges[notification.user_id], trip_titles.get(notification.trip_id)
        )
        for device in devices.get(notification.user_id, []):
            if device.id in dead:
                continue
            try:
                response = client.send(
                    device.token, content, collapse_key=notification.dedupe_key
                )
            except Exception:
                logger.exception("APNs unreachable for device %s", device.id)
                continue
            if response.ok:
                accepted += 1
            elif response.token_is_dead:
                dead.add(device.id)
            else:
                logger.warning(
                    "APNs refused notification %s for device %s: %s %s",
                    notification.id,
                    device.id,
                    response.status,
                    response.reason,
                )
    if dead:
        session.execute(delete(PushDevice).where(PushDevice.id.in_(dead)))
        session.commit()
    return accepted
