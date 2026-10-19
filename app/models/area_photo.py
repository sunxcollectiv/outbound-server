from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.area import Area


class AreaPhoto(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A curated photo for an area, sourced from a third party (e.g. Unsplash).

    We store the source's hotlink image URL rather than copying the bytes — the
    photo lives on the provider's CDN and we point at it. ``source`` and the
    ``attribution`` fields keep the credit the provider's terms require (and let
    us ping the provider's download endpoint later via ``source_id``). An area
    holds a small, ordered handful (``position`` 0, 1, 2…); the first usually
    becomes the area's ``default_photo_url``.
    """

    __tablename__ = "area_photos"
    __table_args__ = (
        # One row per (area, provider photo): makes the backfill idempotent.
        UniqueConstraint("area_id", "source", "source_id", name="uq_area_photos_area_source_id"),
    )

    area_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("areas.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    source: Mapped[str] = mapped_column(String(60))
    source_id: Mapped[str | None] = mapped_column(String(255))
    attribution: Mapped[str | None] = mapped_column(String(500))
    attribution_url: Mapped[str | None] = mapped_column(String(2048))
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(16))

    area: Mapped[Area] = relationship(back_populates="photos")
