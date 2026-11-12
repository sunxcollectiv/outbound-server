"""Seed the places table with the world's airports that have an IATA code and
scheduled commercial service (about 4,100), so an airport can be found before
anyone has flown through it.

Run it any time — idempotent:

    uv run python -m app.seeds.airports

Each airport is a Place with ``category="airport"`` and its IATA code in
``code``. An airport is skipped when a place with that category and code already
exists, so re-running adds only what the list has gained and never edits a row
that is already there (a renamed airport keeps the name you gave it).

The list lives in app.seeds._airports_data (generated from OurAirports by
scripts/generate_airports.py). Each airport is linked to the area for its city
when we have one under the same country, and left without an area otherwise, so
run (or let this run) the area seeds first.
"""
from __future__ import annotations

import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Area, Place
from app.seeds import cities, countries, regions
from app.seeds._airports_data import AIRPORTS

CATEGORY = "airport"


def _fold(name: str) -> str:
    """Accent- and case-insensitive key ("Cancún" == "cancun")."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _city_candidates(city: str) -> list[str]:
    """The names to try for an OurAirports municipality, most specific first.
    Its free text often qualifies the city: "Paris (Roissy-en-France)",
    "Kuta, Badung", "Istanbul(Bakırköy)"."""
    candidates = [city, city.split("(")[0], city.split(",")[0], city.split("/")[0]]
    return list(dict.fromkeys(c.strip() for c in candidates if c.strip()))


def seed_airports(session: Session) -> dict[str, int]:
    """Insert any missing airports. Idempotent; returns counts. ``linked`` is
    how many of the created airports were matched to an area."""
    parents = {
        name.casefold(): area_id
        for area_id, name in session.execute(
            select(Area.id, Area.name).where(Area.kind.in_(("country", "territory")))
        ).all()
    }
    # Every child area under a country, of any kind, keyed for lookup by name.
    # First one wins when a country has two areas with the same folded name.
    areas: dict[tuple[str, uuid.UUID], uuid.UUID] = {}
    for area_id, name, parent_id in session.execute(
        select(Area.id, Area.name, Area.parent_id)
        .where(Area.parent_id.is_not(None))
        .order_by(Area.created_at, Area.id)
    ).all():
        areas.setdefault((_fold(name), parent_id), area_id)

    existing = set(
        session.scalars(
            select(Place.code).where(Place.category == CATEGORY, Place.code.is_not(None))
        )
    )

    created = 0
    linked = 0
    for code, name, city, country, latitude, longitude, timezone in AIRPORTS:
        if code in existing:
            continue
        area_id = None
        parent_id = parents.get(country.casefold()) if country else None
        if city and parent_id is not None:
            for candidate in _city_candidates(city):
                area_id = areas.get((_fold(candidate), parent_id))
                if area_id is not None:
                    break
        session.add(
            Place(
                name=name,
                category=CATEGORY,
                code=code,
                address=", ".join(part for part in (city, country) if part) or None,
                latitude=latitude,
                longitude=longitude,
                timezone=timezone,
                area_id=area_id,
            )
        )
        existing.add(code)
        created += 1
        linked += area_id is not None

    session.commit()
    total = len(AIRPORTS)
    return {"created": created, "skipped": total - created, "linked": linked, "total": total}


def main() -> None:
    with SessionLocal() as session:
        # Areas first, so airports can be linked to their city; all idempotent.
        countries.seed_areas(session)
        cities.seed_cities(session)
        regions.seed_regions(session)
        result = seed_airports(session)
    print(
        f"Seeded airports: created {result['created']} "
        f"({result['linked']} linked to an area), "
        f"skipped {result['skipped']} already present "
        f"(of {result['total']} in the list)."
    )


if __name__ == "__main__":
    main()
