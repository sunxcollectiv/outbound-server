from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
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


class TripItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An entry on a trip's itinerary.

    Times are kept the way a ticket prints them: naive wall time plus the IANA
    zone it's read in, one pair per end, so a flight departs in its origin's
    zone and lands in its destination's. The `*_utc` columns hold the matching
    instants for ordering and are derived on every flush (see sync_instants);
    nothing writes them directly.
    """

    __tablename__ = "trip_items"
    __table_args__ = (
        # Instants, not wall times: a westbound flight can land at an earlier
        # local time than it took off.
        CheckConstraint(
            "ends_at_utc IS NULL OR ends_at_utc >= starts_at_utc", name="instants_ordered"
        ),
        CheckConstraint(
            "(ends_at IS NULL) = (end_timezone IS NULL) AND (ends_at IS NULL) = (ends_at_utc IS NULL)",
            name="end_has_timezone",
        ),
        CheckConstraint(
            "(scope = 'group') = (requirement IS NOT NULL)", name="group_has_requirement"
        ),
        CheckConstraint(
            "(scope = 'personal') = (visibility IS NOT NULL)", name="personal_has_visibility"
        ),
        Index("ix_trip_items_trip_id_starts_at_utc", "trip_id", "starts_at_utc"),
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
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), default=list, server_default=text("'{}'")
    )
    # Local wall time at each end, with the IANA zone it's read in.
    starts_at: Mapped[datetime] = mapped_column(LocalDateTime)
    start_timezone: Mapped[str] = mapped_column(String(64))
    ends_at: Mapped[datetime | None] = mapped_column(LocalDateTime)
    end_timezone: Mapped[str | None] = mapped_column(String(64))
    # Derived instants; see sync_instants.
    starts_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    def sync_instants(self) -> None:
        """Recompute the UTC instants from the local time + zone pairs. Reads
        only this row's columns so it's safe inside a flush."""
        if self.starts_at.tzinfo is not None or (
            self.ends_at is not None and self.ends_at.tzinfo is not None
        ):
            raise ValueError("trip item times must be naive local wall times")
        self.starts_at_utc = to_utc(self.starts_at, self.start_timezone)
        self.ends_at_utc = (
            to_utc(self.ends_at, self.end_timezone) if self.ends_at is not None else None
        )


@event.listens_for(TripItem, "before_insert")
@event.listens_for(TripItem, "before_update")
def _sync_instants(mapper: object, connection: object, item: TripItem) -> None:
    item.sync_instants()


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
