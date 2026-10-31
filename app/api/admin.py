"""Admin/management APIs for the staff dashboard. Every route is gated by the
`AdminUser` dependency (403 for non-admins) and its summary is prefixed
`[admin]`. This is the home for dashboard read models and, as they're needed,
the other admin-only endpoints the React dashboard calls."""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, SessionDep
from app.models import User
from app.schemas import AdminUserPage, AdminUserRead

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=AdminUserPage, summary="[admin] List users")
def list_users(
    _: AdminUser,
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
