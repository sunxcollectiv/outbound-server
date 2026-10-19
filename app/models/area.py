import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Area(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A place-on-the-map a specific spot sits in, e.g. "Paris".

    A custom, flexible label several places can share. Areas span every grain of
    geography we care about — countries, cities, colloquial regions,
    neighborhoods, boroughs — so the taxonomy stays open:

    - ``kind``: a free-form string naming what sort of area this is
      ("country", "city", "region", "neighborhood", "borough", ...). Deliberately
      not an enum so new kinds don't need a migration.
    - ``level``: a rough number for how broad/narrow the area is, meant to drive
      prioritization and nesting later (e.g. country < region < city <
      neighborhood). Exactly how levels get compared is still TBD.
    - ``parent_id``: an optional self-reference to the area this one sits inside
      (a city's country, say). Self-referential so the hierarchy is arbitrary
      depth — a province layer can later slot between city and country with no
      schema change. Cleared (SET NULL) if the parent is deleted.

    Fields are optional for now: areas may be derived by the system (e.g. from a
    place's address) as well as authored, so there's no created_by — just the
    created_at/updated_at timestamps from TimestampMixin. A richer shape (e.g. a
    boundary) may come later.
    """

    __tablename__ = "areas"

    name: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str | None] = mapped_column(String(60), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("areas.id", ondelete="SET NULL"), index=True
    )
    level: Mapped[int | None] = mapped_column(Integer)
    photo_url: Mapped[str | None] = mapped_column(String(2048))

    parent: Mapped["Area | None"] = relationship(remote_side="Area.id")
