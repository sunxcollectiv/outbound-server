from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.area import Area


class Place(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A specific spot in our catalogue (a hotel, a station, the Champs-Élysées),
    optionally linked to a Google Maps place.

    The columns above the Google block are ours to edit. google_place_id is the
    durable link; google_data caches the Place Details response and is replaced
    wholesale on refresh. area_id points at the area the place sits
    in — e.g. Paris — which is friendlier than a precise POI or township for display.
    """

    __tablename__ = "places"

    name: Mapped[str | None] = mapped_column(String(200), index=True)
    category: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(String(2048))
    address: Mapped[str | None] = mapped_column(String(500))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str | None] = mapped_column(String(64))

    area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), index=True
    )

    google_place_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    google_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    google_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    area: Mapped[Area | None] = relationship()
