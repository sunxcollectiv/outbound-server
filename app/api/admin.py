"""Admin/management APIs for the staff dashboard.

This is the single home for everything privileged: the management endpoints
defined here (users, broadcast) plus the fully-admin resource routers included
at the bottom (announcements, received mail/images). The admin gate is applied
once, where this router is mounted (`app.include_router(admin.router,
dependencies=[Depends(get_admin_user)])` in app/main.py), so every route under
`/admin/*` is admin-only by construction — no per-endpoint check to forget.
Included routers keep their own resource tags, so the Swagger grouping stays
intact while the path prefix makes the locked-down surface obvious."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api import announcements, received_images, received_mail
from app.api.deps import SessionDep
from app.models import Trip, TripItem, User
from app.schemas import (
    AdminUserPage,
    AdminUserRead,
    NotificationSend,
    NotificationSendResult,
)
from app.services.notifications import notify

router = APIRouter(prefix="/admin")


@router.get("/users", response_model=AdminUserPage, tags=["admin"], summary="[admin] List users")
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
        items=[AdminUserRead.model_validate(user) for user in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/notifications",
    response_model=NotificationSendResult,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
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


# Fully-admin resource routers, mounted under /admin/* (they keep their own
# prefixes and tags). The admin gate is applied where this router is included.
router.include_router(announcements.router)
router.include_router(received_mail.router)
router.include_router(received_images.router)
