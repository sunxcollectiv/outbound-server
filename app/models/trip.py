from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.place import Place
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
    # No timezone here: a trip can cross zones, so each item carries its own
    # (see TripItem.start_timezone / end_timezone).
    cover_image_url: Mapped[str | None] = mapped_column(String(2048))
    # Areas (countries/cities) this trip covers, in order. No FK: ids are
    # validated in the API, not the database.
    area_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default=text("'{}'")
    )

    owner: Mapped[User] = relationship()
    members: Mapped[list[TripMember]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", passive_deletes=True
    )
    items: Mapped[list[TripItem]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", passive_deletes=True
    )
    days: Mapped[list[TripDay]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TripDay.date",
    )


class TripDay(TimestampMixin, Base):
    """One row per calendar date of a trip, kept in step with its dates (see
    app.services.trip_days.sync_trip_days). The scaffolding for a day before
    anything is booked: where the group expects to be and which zones the day
    runs in, so an item added without a zone can take the first one. A day
    can list several zones for a border crossed and re-crossed. They are a
    default for new items, not a claim about every item on the day: an item
    with a place or an explicit zone keeps its own.

    `name` is the organizer's label for the day ("Wedding Day"); `place` is
    typically a city-level Google place.
    """

    __tablename__ = "trip_days"

    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    # IANA names; the first is the default for items added to this day.
    timezones: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, server_default=text("'{}'")
    )
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), index=True
    )

    trip: Mapped[Trip] = relationship(back_populates="days")
    place: Mapped[Place | None] = relationship()

    @property
    def day_number(self) -> int:
        return (self.date - self.trip.start_date).days + 1


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
