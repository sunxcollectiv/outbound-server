from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import LocalDateTime, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.place import Place
from app.models.trip import Trip
from app.models.user import User
from app.services.timekeeping import to_utc


class TripItemKind(StrEnum):
    LODGING = "lodging"  # starts_at = check-in, ends_at = check-out
    ACTIVITY = "activity"
    # place -> end_place; starts_at = departure, ends_at = arrival. A
    # member's own transit is how the group learns when they come and go.
    TRANSIT = "transit"


class TripItemScope(StrEnum):
    GROUP = "group"  # posted by an organizer, for everyone on the trip
    PERSONAL = "personal"  # posted by a member, for themself (and whoever they let in)


class TripItemRequirement(StrEnum):
    """How much the organizer expects everyone at a group item. A label, not
    an enforcement: people can still decline, and the organizer sees that."""

    MANDATORY = "mandatory"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class TripItemVisibility(StrEnum):
    """Who else on the trip can see a personal item."""

    PRIVATE = "private"  # only the creator
    SHARED = "shared"  # everyone can see where I'll be
    OPEN = "open"  # shared, and others may join


class RsvpStatus(StrEnum):
    GOING = "going"
    MAYBE = "maybe"
    DECLINED = "declined"


class LocalTimeSpan:
    """A start and an optional end, each a naive wall time plus, when known, the
    IANA zone it's read in — the way a ticket prints them, so a flight departs in
    its origin's zone and lands in its destination's. No zone ("dinner at 7 on
    the 17th") is a floating time: shown as written, the client picking a zone
    only when it needs an instant. The `*_utc` columns hold the instants of the
    zoned ends, for ordering, and are derived on every flush by sync_instants;
    nothing writes them directly. Mixed into the models that carry such a span
    (see local_time_span_constraints and register_instant_sync)."""

    starts_at: Mapped[datetime] = mapped_column(LocalDateTime)
    start_timezone: Mapped[str | None] = mapped_column(String(64))
    ends_at: Mapped[datetime | None] = mapped_column(LocalDateTime)
    end_timezone: Mapped[str | None] = mapped_column(String(64))
    starts_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def sync_instants(self) -> None:
        """Recompute the UTC instants from the local time + zone pairs; a
        floating end has none. Reads only this row's columns so it's safe inside
        a flush."""
        if self.starts_at.tzinfo is not None or (
            self.ends_at is not None and self.ends_at.tzinfo is not None
        ):
            raise ValueError("times must be naive local wall times")
        self.starts_at_utc = (
            to_utc(self.starts_at, self.start_timezone) if self.start_timezone else None
        )
        self.ends_at_utc = (
            to_utc(self.ends_at, self.end_timezone)
            if self.ends_at is not None and self.end_timezone
            else None
        )


def local_time_span_constraints() -> tuple[CheckConstraint, ...]:
    """The invariants a LocalTimeSpan's columns must hold, for a table's args."""
    return (
        # Zoned ends are ordered by instant: a westbound flight can land at an
        # earlier local time than it took off. Floating ones by wall time.
        CheckConstraint(
            "ends_at_utc IS NULL OR ends_at_utc >= starts_at_utc", name="instants_ordered"
        ),
        CheckConstraint(
            "start_timezone IS NOT NULL OR ends_at IS NULL OR ends_at >= starts_at",
            name="floating_ordered",
        ),
        # A zoned start with an end has a zoned end; otherwise no end zone. An
        # instant exists exactly when its zone does.
        CheckConstraint(
            "(start_timezone IS NOT NULL AND ends_at IS NOT NULL) = (end_timezone IS NOT NULL)"
            " AND (start_timezone IS NULL) = (starts_at_utc IS NULL)"
            " AND (end_timezone IS NULL) = (ends_at_utc IS NULL)",
            name="zones_consistent",
        ),
    )


def register_instant_sync(*models: type[LocalTimeSpan]) -> None:
    """Keep each model's `*_utc` instants in step with its local times on flush."""
    for model in models:
        event.listen(model, "before_insert", _sync_instants)
        event.listen(model, "before_update", _sync_instants)


def _sync_instants(mapper: object, connection: object, target: LocalTimeSpan) -> None:
    target.sync_instants()


class TripItem(UUIDPrimaryKeyMixin, TimestampMixin, LocalTimeSpan, Base):
    """An entry on a trip's itinerary. Its times are a LocalTimeSpan: for a
    lodging, check-in to check-out; for a transit, the whole booking's departure
    to final arrival (with the legs in transit_segments)."""

    __tablename__ = "trip_items"
    __table_args__ = (
        *local_time_span_constraints(),
        CheckConstraint(
            "(scope = 'group') = (requirement IS NOT NULL)", name="group_has_requirement"
        ),
        CheckConstraint(
            "(scope = 'personal') = (visibility IS NOT NULL)", name="personal_has_visibility"
        ),
        Index("ix_trip_items_trip_id_starts_at", "trip_id", "starts_at"),
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    kind: Mapped[TripItemKind] = mapped_column(str_enum(TripItemKind, "trip_item_kind"))
    scope: Mapped[TripItemScope] = mapped_column(str_enum(TripItemScope, "trip_item_scope"))
    # Set exactly when scope is group.
    requirement: Mapped[TripItemRequirement | None] = mapped_column(
        str_enum(TripItemRequirement, "trip_item_requirement")
    )
    # Set exactly when scope is personal.
    visibility: Mapped[TripItemVisibility | None] = mapped_column(
        str_enum(TripItemVisibility, "trip_item_visibility")
    )
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    # Booking reference / record locator for the whole item — a flight PNR, a
    # hotel confirmation number, etc. Null for items that aren't a booking.
    confirmation_number: Mapped[str | None] = mapped_column(String(120))
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), default=list, server_default=text("'{}'")
    )
    # starts_at/ends_at (+ zones, + derived *_utc) come from LocalTimeSpan.
    image_url: Mapped[str | None] = mapped_column(String(2048))

    place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), index=True
    )
    end_place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL")
    )
    # For personal items this is the owner: the only person who may edit or
    # delete it, and the only one who can see it when it's private.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    trip: Mapped[Trip] = relationship(back_populates="items")
    place: Mapped[Place | None] = relationship(foreign_keys=[place_id])
    end_place: Mapped[Place | None] = relationship(foreign_keys=[end_place_id])
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    participants: Mapped[list[TripItemParticipant]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TripItemParticipant.created_at",
    )
    # A transit booking's legs, in order. Always at least one for transit (a
    # plain hop is a single leg); empty for every other kind. The item's own
    # span (starts_at/ends_at, place/end_place) is kept as the first leg's
    # departure and the last leg's arrival, so the itinerary and every
    # cross-kind sort keep reading one row; the per-leg detail and the layovers
    # between legs live here.
    transit_segments: Mapped[list[TripItemTransitSegment]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TripItemTransitSegment.position",
    )


class TripItemTransitSegment(UUIDPrimaryKeyMixin, TimestampMixin, LocalTimeSpan, Base):
    """One leg of a transit booking: a single vehicle movement from one place to
    another, its times a LocalTimeSpan like the item's. A leg always has a
    departure; its arrival may be unknown (`ends_at` null). The gap between one
    leg's arrival and the next's departure is a layover, which the itinerary
    derives — it is never stored.
    """

    __tablename__ = "trip_item_transit_segments"
    __table_args__ = (
        *local_time_span_constraints(),
        # Deferred so replacing a booking's legs (delete old, insert new at the
        # same positions) doesn't trip the constraint mid-flush.
        UniqueConstraint(
            "trip_item_id",
            "position",
            name="uq_transit_segment_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_transit_segments_item_position", "trip_item_id", "position"),
    )

    trip_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_items.id", ondelete="CASCADE")
    )
    # 0-based order of the leg within the booking.
    position: Mapped[int] = mapped_column(SmallInteger)
    # starts_at/ends_at (+ zones, + derived *_utc) come from LocalTimeSpan.
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL")
    )
    end_place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL")
    )
    # Operator and service number for the leg, e.g. ("ANA", "NH7").
    carrier: Mapped[str | None] = mapped_column(String(120))
    flight_number: Mapped[str | None] = mapped_column(String(40))

    item: Mapped[TripItem] = relationship(back_populates="transit_segments")
    place: Mapped[Place | None] = relationship(foreign_keys=[place_id])
    end_place: Mapped[Place | None] = relationship(foreign_keys=[end_place_id])


register_instant_sync(TripItem, TripItemTransitSegment)


class TripItemParticipant(TimestampMixin, Base):
    """Who's in on an item.

    On a group item a row is an RSVP (no row = hasn't answered). On a personal
    item the rows are the people attending: the creator is added as going when
    the item is made, and others can add themselves only if it's open.
    """

    __tablename__ = "trip_item_participants"

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_items.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[RsvpStatus] = mapped_column(str_enum(RsvpStatus, "rsvp_status"))

    item: Mapped[TripItem] = relationship(back_populates="participants")
    user: Mapped[User] = relationship()
