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
    UniqueConstraint,
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
from app.services.pricing import CURRENT_SCHEDULE, price, trip_days

if TYPE_CHECKING:
    from app.models.trip_item import TripItem


class TripRole(StrEnum):
    ORGANIZER = "organizer"  # posts group items, manages members
    MEMBER = "member"  # adds their own items, RSVPs


class TripGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A cluster of related trips: a trip and the trips extended off it (and
    off those, without limit). Undirected: it answers "fetch everything in
    this cluster" for merged itineraries without walking the parent links,
    and it outlives any one trip in it, so a chain stays grouped when the
    trip in the middle is deleted. Standalone trips have no group. Nothing
    lives here yet besides the id; a name and a group-level thread can land
    later without touching trips."""

    __tablename__ = "trip_groups"

    trips: Mapped[list[Trip]] = relationship(
        back_populates="group", order_by=lambda: (Trip.start_date, Trip.created_at)
    )


class Trip(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A trip, or an extension of one. An extension is a full trip in its own
    right (own organizer, roster, days, items, bill) that was created by
    extending another: `extends_trip_id` records that, and both trips share
    a `group_id`. The two say different things. The parent link is directed
    provenance, used to decide who may extend (members of the parent), that
    the extension touches its parent's dates, and which trip owns a date the
    two share; it is cleared if the parent goes. The group is the undirected
    cluster the two belong to, used for merged itineraries; it survives the
    parent. A trip in a group with no parent is one whose parent was deleted."""

    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="dates_ordered"),
        # A parent link only makes sense inside a group, and never to itself.
        CheckConstraint(
            "extends_trip_id IS NULL OR group_id IS NOT NULL", name="extension_in_group"
        ),
        CheckConstraint("extends_trip_id IS DISTINCT FROM id", name="extension_not_self"),
    )

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
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trip_groups.id", ondelete="SET NULL"), index=True
    )
    extends_trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL"), index=True
    )
    # Which rate table this trip is priced under (app.services.pricing). Set
    # once at creation, so later rate changes don't reprice it.
    price_schedule: Mapped[str] = mapped_column(
        String(16), default=CURRENT_SCHEDULE, server_default=CURRENT_SCHEDULE
    )

    owner: Mapped[User] = relationship()
    group: Mapped[TripGroup | None] = relationship(back_populates="trips")
    extends: Mapped[Trip | None] = relationship(
        remote_side="Trip.id", back_populates="extensions", foreign_keys=[extends_trip_id]
    )
    # Trips created by extending this one, directly.
    extensions: Mapped[list[Trip]] = relationship(
        back_populates="extends",
        foreign_keys=[extends_trip_id],
        order_by=lambda: (Trip.start_date, Trip.created_at),
    )
    members: Mapped[list[TripMember]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (TripMember.created_at, TripMember.id),
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

    @property
    def price_cents(self) -> int:
        """What the trip costs today under its schedule: its length and
        roster. Not what has been paid for it (see app.services.credits)."""
        days = trip_days(self.start_date, self.end_date)
        return price(days, len(self.members), self.price_schedule)


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


class TripMember(UUIDPrimaryKeyMixin, Base):
    """Everyone on a trip, owner included, with their role. trips.owner_id
    only says who pays for and can delete the trip; it always has an
    organizer row here, so counting rows gives the real headcount and a
    transfer of ownership is a role edit, not a row swap.

    A member is a person on the trip, not necessarily an account: organizers
    add people by email, and the row stays on the roster whether or not that
    person ever signs up, so the organizer can plan around them (RSVPs are
    keyed by member, not user). `user_id` is filled in as soon as an account
    with that email exists, at add time or when they register, and from then
    on that account can open the trip."""

    __tablename__ = "trip_members"
    __table_args__ = (
        UniqueConstraint("trip_id", "email", name="uq_trip_members_trip_id_email"),
        UniqueConstraint("trip_id", "user_id", name="uq_trip_members_trip_id_user_id"),
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    # Stored lowercased. The address they were added at; for a linked member
    # it matches their account's email.
    email: Mapped[str] = mapped_column(String(320), index=True)
    # Null until an account with this email exists. SET NULL so the person
    # stays on the trip if the account goes away.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[TripRole] = mapped_column(
        str_enum(TripRole, "trip_role"), default=TripRole.MEMBER
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    trip: Mapped[Trip] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship()
