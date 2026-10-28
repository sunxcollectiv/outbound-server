import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.models import (
    Notification,
    NotificationCategory,
    NotificationPreference,
    PushDevice,
    Trip,
    TripItem,
    User,
)
from app.schemas import (
    NotificationPage,
    NotificationPreferenceRead,
    NotificationPreferenceSet,
    NotificationRead,
    NotificationSend,
    NotificationSendResult,
    PushDeviceCreate,
    PushDeviceRead,
    UnreadCount,
)
from app.services.notifications import (
    LOCKED,
    in_inbox,
    inbox_query,
    notify,
    preference_matrix,
    sync_announcements,
    unread_count,
)

# The fixed paths (unread-count, read-all, preferences, devices) are declared
# before /{notification_id} so they aren't captured as an id.
router = APIRouter(prefix="/notifications", tags=["notifications"])


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")


def _get_notification(session: Session, user: User, notification_id: uuid.UUID) -> Notification:
    """One of the caller's notifications that has arrived. Anyone else's, or
    one not yet due, is a 404."""
    notification = session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user.id,
            Notification.visible_at <= func.now(),
        )
    )
    if notification is None:
        raise _not_found()
    return notification


@router.get("", response_model=NotificationPage)
def list_notifications(
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    before: Annotated[
        uuid.UUID | None, Query(description="notification-id cursor: older than this one")
    ] = None,
    unread_only: bool = False,
    category: NotificationCategory | None = None,
) -> NotificationPage:
    """The caller's inbox, newest first, a page at a time: pass the returned
    `next_cursor` as `before` to go further back."""
    sync_announcements(session, user)
    session.commit()

    query = inbox_query(session, user, unread_only=unread_only, category=category)
    if before is not None:
        anchor = session.scalar(
            select(Notification).where(
                Notification.id == before, Notification.user_id == user.id
            )
        )
        if anchor is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown cursor")
        query = query.where(
            tuple_(Notification.visible_at, Notification.id)
            < tuple_(anchor.visible_at, anchor.id)
        )
    rows = list(session.scalars(query.limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    return NotificationPage(
        items=[NotificationRead.model_validate(row) for row in page],
        next_cursor=page[-1].id if more else None,
    )


@router.post("", response_model=NotificationSendResult, status_code=status.HTTP_201_CREATED)
def send_notification(
    payload: NotificationSend, _: AdminUser, session: SessionDep
) -> NotificationSendResult:
    """Admins only: put a notification in particular people's inboxes, e.g. a
    support message or to try out how one looks. Unknown ids or emails are a
    422 and nothing is sent."""
    recipients: set[uuid.UUID] = set()
    if payload.user_ids:
        found = set(session.scalars(select(User.id).where(User.id.in_(payload.user_ids))))
        if missing := [str(i) for i in payload.user_ids if i not in found]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"No account with id: {', '.join(missing)}"
            )
        recipients |= found
    if payload.emails:
        by_email = {
            email: user_id
            for email, user_id in session.execute(
                select(User.email, User.id).where(User.email.in_(payload.emails))
            )
        }
        if missing := [e for e in payload.emails if e not in by_email]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"No account with email: {', '.join(missing)}",
            )
        recipients |= set(by_email.values())
    for field, model in (("trip_id", Trip), ("trip_item_id", TripItem)):
        value = getattr(payload, field)
        if value is not None and session.get(model, value) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} does not match anything"
            )

    content = payload.model_dump(exclude={"user_ids", "emails"})
    sent = notify(session, user_ids=recipients, **content)
    session.commit()
    return NotificationSendResult(recipients=len(recipients), sent=sent)


@router.get("/unread-count", response_model=UnreadCount)
def read_unread_count(user: CurrentUser, session: SessionDep) -> UnreadCount:
    """The badge: unread notifications in the inbox."""
    sync_announcements(session, user)
    session.commit()
    return UnreadCount(count=unread_count(session, user))


@router.post("/read-all", response_model=UnreadCount)
def mark_all_read(
    user: CurrentUser, session: SessionDep, category: NotificationCategory | None = None
) -> UnreadCount:
    """Mark everything in the inbox read (or everything in one category).
    Returns what's left unread."""
    query = update(Notification).where(*in_inbox(session, user), Notification.read_at.is_(None))
    if category is not None:
        query = query.where(Notification.category == category)
    session.execute(query.values(read_at=func.now()))
    session.commit()
    return UnreadCount(count=unread_count(session, user))


# --- preferences ---


def _preferences(session: Session, user: User) -> list[NotificationPreferenceRead]:
    return [
        NotificationPreferenceRead(
            category=category, channel=channel, enabled=on, locked=(category, channel) in LOCKED
        )
        for (category, channel), on in preference_matrix(session, user).items()
    ]


@router.get("/preferences", response_model=list[NotificationPreferenceRead])
def read_preferences(user: CurrentUser, session: SessionDep) -> list[NotificationPreferenceRead]:
    """Every category on every channel, with the caller's choice or the default."""
    return _preferences(session, user)


@router.put("/preferences", response_model=list[NotificationPreferenceRead])
def set_preferences(
    payload: list[NotificationPreferenceSet], user: CurrentUser, session: SessionDep
) -> list[NotificationPreferenceRead]:
    """Set any of the switches; ones left out keep their value. A locked switch
    can be sent, but only as it is."""
    for entry in payload:
        if (entry.category, entry.channel) in LOCKED and not entry.enabled:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{entry.category} on {entry.channel} can't be turned off",
            )
    rows = [
        {"user_id": user.id, **entry.model_dump()}
        for entry in payload
        if (entry.category, entry.channel) not in LOCKED
    ]
    if rows:
        statement = insert(NotificationPreference).values(rows)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id", "category", "channel"],
                set_={"enabled": statement.excluded.enabled, "updated_at": func.now()},
            )
        )
    session.commit()
    return _preferences(session, user)


# --- push devices ---


@router.get("/devices", response_model=list[PushDeviceRead])
def list_devices(user: CurrentUser, session: SessionDep) -> list[PushDevice]:
    return list(
        session.scalars(
            select(PushDevice).where(PushDevice.user_id == user.id).order_by(PushDevice.created_at)
        )
    )


@router.post("/devices", response_model=PushDeviceRead)
def register_device(
    payload: PushDeviceCreate, user: CurrentUser, session: SessionDep
) -> PushDevice:
    """Register this device's push token; call on every launch after login.
    A token already registered is updated, and moves to the caller if another
    account had it (a shared or handed-down phone)."""
    device = session.scalar(select(PushDevice).where(PushDevice.token == payload.token))
    if device is None:
        device = PushDevice(token=payload.token)
        session.add(device)
    device.user_id = user.id
    device.platform = payload.platform
    device.app_version = payload.app_version
    device.locale = payload.locale
    device.timezone = payload.timezone
    device.last_seen_at = datetime.now(UTC)
    session.commit()
    session.refresh(device)
    return device


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(device_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    """Forget a device, e.g. on logout."""
    device = session.get(PushDevice, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    session.delete(device)
    session.commit()


# --- one notification ---


@router.post("/{notification_id}/read", response_model=NotificationRead)
def mark_read(notification_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Notification:
    notification = _get_notification(session, user, notification_id)
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        session.commit()
    return notification


@router.delete("/{notification_id}/read", response_model=NotificationRead)
def mark_unread(
    notification_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Notification:
    notification = _get_notification(session, user, notification_id)
    notification.read_at = None
    session.commit()
    return notification


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive(notification_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> None:
    """Dismiss a notification from the inbox. It's kept, out of sight, so a
    dismissed announcement doesn't come back."""
    notification = _get_notification(session, user, notification_id)
    if notification.archived_at is None:
        notification.archived_at = datetime.now(UTC)
        session.commit()
