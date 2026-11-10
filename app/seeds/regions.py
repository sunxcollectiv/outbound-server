"""Seed the areas table with islands and regions (Bali, Tuscany, Lake Tahoe),
each linked to its parent country/territory area via ``parent_id``.

Run it any time — idempotent:

    uv run python -m app.seeds.regions

The list lives in app.seeds._regions_data. An entry is skipped when an area with
the same name (ignoring case and accents) already sits under the same parent,
whatever its kind: Santorini and Phuket were seeded as cities, and a second row
would just be a duplicate in search. Parents are looked up by name, so run (or
let this run) the countries seed first.
"""
from __future__ import annotations

import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Area
from app.seeds import countries
from app.seeds._regions_data import REGIONS


def _fold(name: str) -> str:
    """Accent- and case-insensitive key ("Roatán" == "roatan")."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def seed_regions(session: Session) -> dict[str, int]:
    """Insert any missing island/region areas under their parent
    country/territory. Idempotent; returns counts. Entries whose parent area is
    missing are skipped and counted under ``unresolved``."""
    parents = {
        name.casefold(): area_id
        for area_id, name in session.execute(
            select(Area.id, Area.name).where(Area.kind.in_(("country", "territory")))
        ).all()
    }
    # Every child area, of any kind, so a name already used under a parent is
    # never added twice.
    existing = {
        (_fold(name), parent_id)
        for name, parent_id in session.execute(
            select(Area.name, Area.parent_id).where(Area.parent_id.is_not(None))
        ).all()
    }

    created = 0
    unresolved = 0
    for name, kind, country in REGIONS:
        parent_id = parents.get(country.casefold())
        if parent_id is None:
            unresolved += 1
            continue
        key = (_fold(name), parent_id)
        if key in existing:
            continue
        session.add(Area(name=name, kind=kind, parent_id=parent_id))
        existing.add(key)
        created += 1

    session.commit()
    total = len(REGIONS)
    return {
        "created": created,
        "skipped": total - created - unresolved,
        "unresolved": unresolved,
        "total": total,
    }


def main() -> None:
    with SessionLocal() as session:
        # Parents must exist first; seeding countries is itself idempotent.
        countries.seed_areas(session)
        result = seed_regions(session)
    msg = (
        f"Seeded regions: created {result['created']}, "
        f"skipped {result['skipped']} already present "
        f"(of {result['total']} in the list)."
    )
    if result["unresolved"]:
        msg += f" {result['unresolved']} skipped — parent country not found."
    print(msg)


if __name__ == "__main__":
    main()
