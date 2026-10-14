from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.user import User

if TYPE_CHECKING:
    from app.models.trip_item import TripItem


class TripRole(StrEnum):
    ORGANIZER = "organizer"  # posts group items, manages members
    MEMBER = "member"  # adds their own items, RSVPs


class Trip(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trips"
    __table_args__ = (CheckConstraint("end_date >= start_date", name="dates_ordered"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # No timezone here: a trip can cross zones, so each item resolves its own
    # (see TripItem.timezone).
    cover_image_url: Mapped[str | None] = mapped_column(String(2048))

    owner: Mapped[User] = relationship()
    members: Mapped[list[TripMember]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", passive_deletes=True
    )
    items: Mapped[list[TripItem]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", passive_deletes=True
    )


class TripMember(Base):
    """Everyone on a trip, owner included, with their role. trips.owner_id
    only says who pays for and can delete the trip; it always has an
    organizer row here, so counting rows gives the real headcount and a
    transfer of ownership is a role edit, not a row swap."""

    __tablename__ = "trip_members"

    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[TripRole] = mapped_column(
        str_enum(TripRole, "trip_role"), default=TripRole.MEMBER
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trip: Mapped[Trip] = relationship(back_populates="members")
    user: Mapped[User] = relationship()
