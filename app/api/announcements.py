import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import AdminUser, SessionDep
from app.models import Announcement
from app.schemas import AnnouncementCreate, AnnouncementRead, AnnouncementUpdate
from app.services.notifications import propagate_announcement

# Managing broadcasts; every route is for admins only.
router = APIRouter(prefix="/announcements", tags=["announcements"])


def _get_announcement(session: SessionDep, announcement_id: uuid.UUID) -> Announcement:
    announcement = session.get(Announcement, announcement_id)
    if announcement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Announcement not found")
    return announcement


@router.post("", response_model=AnnouncementRead, status_code=status.HTTP_201_CREATED)
def create_announcement(
    payload: AnnouncementCreate, admin: AdminUser, session: SessionDep
) -> Announcement:
    """Broadcast to every user. It reaches each inbox the next time that
    inbox is read."""
    values = payload.model_dump(exclude_none=True)
    values.setdefault("type", payload.category.value)
    announcement = Announcement(**values, created_by_id=admin.id)
    session.add(announcement)
    session.commit()
    session.refresh(announcement)
    return announcement


@router.get("", response_model=list[AnnouncementRead])
def list_announcements(_: AdminUser, session: SessionDep) -> list[Announcement]:
    """All announcements, past, running and scheduled; newest start first."""
    return list(
        session.scalars(select(Announcement).order_by(Announcement.starts_at.desc()))
    )


@router.patch("/{announcement_id}", response_model=AnnouncementRead)
def update_announcement(
    announcement_id: uuid.UUID,
    payload: AnnouncementUpdate,
    _: AdminUser,
    session: SessionDep,
) -> Announcement:
    """Edit an announcement; the copies already in inboxes follow."""
    announcement = _get_announcement(session, announcement_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in ("category", "type", "title", "data", "starts_at"):
        if field in changes and changes[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be null")
    starts_at = changes.get("starts_at", announcement.starts_at)
    ends_at = changes.get("ends_at", announcement.ends_at)
    if ends_at is not None and ends_at <= starts_at:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "ends_at must be after starts_at"
        )
    for field, value in changes.items():
        setattr(announcement, field, value)
    session.flush()
    propagate_announcement(session, announcement)
    session.commit()
    session.refresh(announcement)
    return announcement


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_announcement(announcement_id: uuid.UUID, _: AdminUser, session: SessionDep) -> None:
    """Delete an announcement and take it out of every inbox. To stop it
    without erasing it, PATCH `ends_at` instead."""
    session.delete(_get_announcement(session, announcement_id))
    session.commit()
