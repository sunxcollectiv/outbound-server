"""Admin/management APIs for the staff dashboard.

This is the single home for everything privileged: the management endpoints
defined here (users, broadcast) plus the fully-admin resource routers included
at the bottom (announcements, received mail, received-images admin ops). The
admin gate is applied once, where this router is mounted
(`app.include_router(admin.router, dependencies=[Depends(get_admin_principal)])`
in app/main.py) and accepts an admin service token only, so every route under
`/admin/*` is admin-only by construction — no per-endpoint check to forget.
Included routers keep their own resource tags, so the Swagger grouping stays
intact while the path prefix makes the locked-down surface obvious."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.api import announcements, areas, places, received_images, received_mail
from app.api.credits import DEFAULT_PAGE, PageCursor, PageLimit, read_statement
from app.api.deps import SessionDep
from app.models import Notification, NotificationCategory, Trip, TripItem, TripMember, User
from app.schemas import (
    AdminNotificationPage,
    AdminNotificationRead,
    AdminTripPage,
    AdminTripRead,
    AdminUserPage,
    CreditGrant,
    CreditGrantResult,
    CreditsRead,
    NotificationSend,
    NotificationSendResult,
    UserRead,
)
from app.services import credits
from app.services.notifications import notify

router = APIRouter(prefix="/admin")


@router.get("/users", response_model=AdminUserPage, tags=["admin/users"], summary="[admin] List users")
def list_users(
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="filter by email or display name (case-insensitive substring)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminUserPage:
    """Browse every account for the management dashboard, newest first. Optional
    `q` filters by email or display name (case-insensitive substring). `total`
    is the count across all pages, for the pager."""
    filters = []
    if q and (term := q.strip()):
        filters.append(
            or_(
                User.email.icontains(term, autoescape=True),
                User.display_name.icontains(term, autoescape=True),
            )
        )
    total = session.scalar(select(func.count()).select_from(User).where(*filters)) or 0
    rows = session.scalars(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return AdminUserPage(
        items=[UserRead.model_validate(user) for user in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/trips", response_model=AdminTripPage, tags=["admin/trips"], summary="[admin] List trips")
def list_trips(
    session: SessionDep,
    q: Annotated[
        str | None, Query(description="filter by trip title (case-insensitive substring)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminTripPage:
    """Browse every trip for the management dashboard, newest first, with each
    trip's owner email and member count. Optional `q` filters by title
    (case-insensitive substring). `total` is the count across all pages."""
    title_filter = [Trip.title.icontains(q.strip(), autoescape=True)] if q and q.strip() else []
    total = session.scalar(select(func.count()).select_from(Trip).where(*title_filter)) or 0
    member_counts = (
        select(TripMember.trip_id, func.count(TripMember.id).label("n"))
        .group_by(TripMember.trip_id)
        .subquery()
    )
    stmt = (
        select(Trip, User.email, func.coalesce(member_counts.c.n, 0))
        .join(User, User.id == Trip.owner_id)
        .outerjoin(member_counts, member_counts.c.trip_id == Trip.id)
        .where(*title_filter)
        .order_by(Trip.created_at.desc(), Trip.id)
        .limit(limit)
        .offset(offset)
    )
    items = [
        AdminTripRead(
            id=trip.id,
            title=trip.title,
            owner_email=email,
            start_date=trip.start_date,
            end_date=trip.end_date,
            member_count=count,
            created_at=trip.created_at,
        )
        for trip, email, count in session.execute(stmt)
    ]
    return AdminTripPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/notifications",
    response_model=AdminNotificationPage,
    tags=["admin/notifications"],
    summary="[admin] List notifications",
)
def list_notifications(
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="filter by title or recipient email (case-insensitive substring)"),
    ] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="only this recipient's")] = None,
    category: NotificationCategory | None = None,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminNotificationPage:
    """Every notification row across all inboxes, newest first (by
    `visible_at`, when it lands), with its recipient. Unlike a user's inbox
    this includes rows not yet due, expired and dismissed ones, and doesn't
    apply anyone's muted categories. Announcement copies only exist once a
    user's inbox has been read. `total` is the count across all pages."""
    filters = []
    joins_recipient = False
    if q and (term := q.strip()):
        joins_recipient = True
        filters.append(
            or_(
                Notification.title.icontains(term, autoescape=True),
                User.email.icontains(term, autoescape=True),
            )
        )
    if user_id is not None:
        filters.append(Notification.user_id == user_id)
    if category is not None:
        filters.append(Notification.category == category)
    if unread_only:
        filters.append(Notification.read_at.is_(None))

    def scoped(stmt):
        if joins_recipient:
            stmt = stmt.join(User, User.id == Notification.user_id)
        return stmt.where(*filters)

    total = session.scalar(scoped(select(func.count()).select_from(Notification))) or 0
    rows = session.scalars(
        scoped(select(Notification))
        .options(selectinload(Notification.recipient), selectinload(Notification.actor))
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return AdminNotificationPage(
        items=[AdminNotificationRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/notifications",
    response_model=NotificationSendResult,
    status_code=status.HTTP_201_CREATED,
    tags=["admin/notifications"],
    summary="[admin] Send notification",
)
def send_notification(payload: NotificationSend, session: SessionDep) -> NotificationSendResult:
    """Put a notification in particular people's inboxes, e.g. a support
    message or to try out how one looks. Unknown ids or emails are a 422 and
    nothing is sent."""
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


@router.post(
    "/credits/grants",
    response_model=CreditGrantResult,
    status_code=status.HTTP_201_CREATED,
    tags=["admin/credits"],
    summary="[admin] Grant credits",
)
def grant_credits(payload: CreditGrant, session: SessionDep) -> CreditGrantResult:
    """Put credits on one person's balance, by id or email. The only way
    credits come in until store purchases are wired up. An unknown person is
    a 422; a repeated `external_id` is a 409 and nothing is granted."""
    if payload.user_id is not None:
        user = session.get(User, payload.user_id)
        if user is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"No account with id: {payload.user_id}"
            )
    else:
        user = session.scalar(select(User).where(User.email == payload.email))
        if user is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"No account with email: {payload.email}"
            )
    try:
        entry = credits.grant(
            session,
            user.id,
            payload.amount_cents,
            description=payload.description,
            external_id=payload.external_id,
        )
        session.commit()
    except credits.DuplicateExternalId as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return CreditGrantResult(
        entry=credits.statement_line(session, entry), balance_cents=entry.balance_after_cents
    )


@router.get(
    "/credits/{user_id}",
    response_model=CreditsRead,
    tags=["admin/credits"],
    summary="[admin] Read a user's credits",
)
def read_user_credits(
    user_id: uuid.UUID,
    session: SessionDep,
    limit: PageLimit = DEFAULT_PAGE,
    before: PageCursor = None,
) -> CreditsRead:
    """One person's balance and ledger, newest first, paged and shown just as
    they see it at GET /credits."""
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return read_statement(session, user_id, limit, before)


# Fully-admin resource routers, mounted under /admin/* (they keep their own
# prefixes and tags). The admin gate is applied where this router is included.
router.include_router(announcements.router)
router.include_router(received_mail.router)
router.include_router(received_images.admin_router)
router.include_router(areas.admin_router)
router.include_router(places.admin_router)
