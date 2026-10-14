import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Area(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A place-on-the-map a specific spot sits in, e.g. "Paris".

    Deliberately slim for now: a friendly label (and optional photo) that several
    places can share. Places link to it via places.area_id. A richer shape (e.g. a
    boundary) may come later.
    """

    __tablename__ = "areas"

    name: Mapped[str] = mapped_column(String(200), index=True)
    photo_url: Mapped[str | None] = mapped_column(String(2048))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
