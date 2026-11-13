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
scripts/generate_airports.py). Each airport also gets its Google place id from
app.seeds._airport_google_ids when we have one (exported from a database where
the matches were reviewed), so a new server is linked without searching Google.
Only the id is seeded; the address and photos are fetched from Google later by
scripts/link_airports_to_google.py, since Google's terms don't allow storing
those permanently. Until then an airport's address is a "City, Country"
placeholder, which the first Google fetch replaces.

The id is also added to an airport that already exists without one, which is
the one edit the seed makes: it only fills an empty field. An id another place
already holds is skipped (and counted), since the pair would have to be merged. Each airport is linked to the area for its city
when we have one under the same country, and left without an area otherwise, so
run (or let this run) the area seeds first.
"""
from __future__ import annotations

import unicodedata
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Area, Place
from app.seeds import cities, countries, regions
from app.seeds._airport_google_ids import GOOGLE_PLACE_IDS
from app.seeds._airports_data import AIRPORTS

CATEGORY = "airport"


def _placeholder(city: str | None, country: str | None) -> str | None:
    return ", ".join(part for part in (city, country) if part) or None


# The "City, Country" address the seed gives each airport, by code, so the
# first Google fetch can tell it from an address someone typed.
SEEDED_ADDRESSES: dict[str, str | None] = {
    code: _placeholder(city, country) for code, _name, city, country, *_ in AIRPORTS
}


def has_seeded_address(place: Place) -> bool:
    """True when this airport still has the seed's placeholder address."""
    return (
        place.category == CATEGORY
        and place.code is not None
        and place.address is not None
        and place.address == SEEDED_ADDRESSES.get(place.code)
    )


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
    """Insert any missing airports, and give existing ones without a Google id
    the one we have. Idempotent; returns counts. ``linked`` is how many of the
    created airports were matched to an area; ``google_ids`` how many airports
    (new or existing) got a Google place id; ``google_id_conflicts`` how many
    ids were skipped because another place already holds them."""
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

    existing = {
        code: google_place_id
        for code, google_place_id in session.execute(
            select(Place.code, Place.google_place_id).where(
                Place.category == CATEGORY, Place.code.is_not(None)
            )
        ).all()
    }
    taken_ids = set(
        session.scalars(select(Place.google_place_id).where(Place.google_place_id.is_not(None)))
    )

    def usable_id(code: str) -> str | None:
        """Our Google id for this code, if no place holds it yet."""
        nonlocal conflicts
        google_place_id = GOOGLE_PLACE_IDS.get(code)
        if google_place_id is None:
            return None
        if google_place_id in taken_ids:
            conflicts += 1
            return None
        taken_ids.add(google_place_id)
        return google_place_id

    created = 0
    linked = 0
    google_ids = 0
    conflicts = 0
    for code, name, city, country, latitude, longitude, timezone in AIRPORTS:
        if code in existing:
            if existing[code] is None and code in GOOGLE_PLACE_IDS:
                google_place_id = usable_id(code)
                if google_place_id is not None:
                    session.execute(
                        update(Place)
                        .where(Place.category == CATEGORY, Place.code == code)
                        .values(google_place_id=google_place_id)
                    )
                    google_ids += 1
            continue
        area_id = None
        parent_id = parents.get(country.casefold()) if country else None
        if city and parent_id is not None:
            for candidate in _city_candidates(city):
                area_id = areas.get((_fold(candidate), parent_id))
                if area_id is not None:
                    break
        google_place_id = usable_id(code)
        session.add(
            Place(
                name=name,
                category=CATEGORY,
                code=code,
                address=_placeholder(city, country),
                latitude=latitude,
                longitude=longitude,
                timezone=timezone,
                area_id=area_id,
                google_place_id=google_place_id,
            )
        )
        existing[code] = google_place_id
        created += 1
        linked += area_id is not None
        google_ids += google_place_id is not None

    session.commit()
    total = len(AIRPORTS)
    return {
        "created": created,
        "skipped": total - created,
        "linked": linked,
        "google_ids": google_ids,
        "google_id_conflicts": conflicts,
        "total": total,
    }


def main() -> None:
    with SessionLocal() as session:
        # Areas first, so airports can be linked to their city; all idempotent.
        countries.seed_areas(session)
        cities.seed_cities(session)
        regions.seed_regions(session)
        result = seed_airports(session)
    if result["google_id_conflicts"]:
        print(
            f"{result['google_id_conflicts']} Google ids skipped: another place already "
            "has them. Run scripts/link_airports_to_google.py to merge those airports."
        )
    print(
        f"Seeded airports: created {result['created']} "
        f"({result['linked']} linked to an area), "
        f"Google ids applied {result['google_ids']}, "
        f"skipped {result['skipped']} already present "
        f"(of {result['total']} in the list)."
    )


if __name__ == "__main__":
    main()
