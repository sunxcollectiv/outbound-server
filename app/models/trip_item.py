from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.place import Place
from app.models.trip import Trip
from app.models.user import User


class TripItemKind(StrEnum):
    LODGING = "lodging"  # starts_at = check-in, ends_at = check-out
    ACTIVITY = "activity"
    TRANSIT = "transit"  # place -> end_place
    ARRIVAL = "arrival"  # someone joining the trip; traveler says who


class TripItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trip_items"
    __table_args__ = (
        CheckConstraint("ends_at IS NULL OR ends_at >= starts_at", name="times_ordered"),
        Index("ix_trip_items_trip_id_starts_at", "trip_id", "starts_at"),
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"))
    kind: Mapped[TripItemKind] = mapped_column(str_enum(TripItemKind, "trip_item_kind"))
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(60)), default=list, server_default=text("'{}'")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_url: Mapped[str | None] = mapped_column(String(2048))

    place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), index=True
    )
    end_place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL")
    )
    # Who this item is about when it isn't the whole group, e.g. a companion's arrival.
    traveler_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    trip: Mapped[Trip] = relationship(back_populates="items")
    place: Mapped[Place | None] = relationship(foreign_keys=[place_id])
    end_place: Mapped[Place | None] = relationship(foreign_keys=[end_place_id])
    traveler: Mapped[User | None] = relationship(foreign_keys=[traveler_id])
