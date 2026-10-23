"""Resolve an extracted place descriptor (name/address/city/IATA) to a `Place`.

Match-or-create: reuse an existing place only on a **solid name AND address
match** (both present and equal, normalized); anything short of that creates a
new `Place` with `google_place_id` null. We'd rather keep a faithful copy of what
the itinerary said than force it onto a look-alike — so duplicates are expected
and fine (name-only matching would wrongly merge same-named places in different
cities). A real similarity score (e.g. pg_trgm), city→area linking, duplicate
merging, and platform-id attachment are pre-launch work.

Reusable on its own; the mail-import stage calls it while building items, and a
future admin/re-resolve flow can use the same function. Pass a per-call `cache`
so the same place appearing twice in one booking (e.g. a layover airport that is
one leg's arrival and the next's departure) resolves to a single `Place`.
"""
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Place


def _norm(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def resolve_place(
    session: Session,
    descriptor: dict[str, Any] | None,
    *,
    timezone: str | None = None,
    created_by_id: uuid.UUID | None = None,
    cache: dict[tuple[str | None, str | None], uuid.UUID] | None = None,
) -> uuid.UUID | None:
    """Return the id of the Place this descriptor refers to, creating one if no
    existing place matches. None when there's nothing to resolve (no name/address).
    `timezone` seeds a newly-created place (e.g. the leg's zone for an airport)."""
    if not descriptor:
        return None
    name = descriptor.get("name") or descriptor.get("iata_code")
    address = descriptor.get("address")
    key = (_norm(name), _norm(address))
    if key == (None, None):
        return None
    if cache is not None and key in cache:
        return cache[key]

    # Reuse an existing place ONLY on a solid name AND address match; otherwise
    # create a new one. Duplicates are fine (refine with more data later), and a
    # name-only match would wrongly merge same-named places in different cities —
    # staying faithful to the received itinerary matters more than consolidating.
    place_id: uuid.UUID | None = None
    if key[0] is not None and key[1] is not None:
        place_id = session.scalars(
            select(Place.id)
            .where(
                func.lower(func.trim(Place.name)) == key[0],
                func.lower(func.trim(Place.address)) == key[1],
            )
            .limit(1)
        ).first()

    if place_id is None:
        # google_place_id stays null — places no longer require a Google id.
        # TODO(places): a later follow-up (likely async) should try to attach a
        # google_place_id — or another platform's id (e.g. for China) — to these
        # resolver-created places by searching that platform for the name/address,
        # enrich from it, and merge any duplicates the simple match let through.
        # Keep that out of the request path; this resolver stays a fast local
        # match-or-create. See places-are-google-backed-stubs (pre-launch TODOs).
        place = Place(
            name=name, address=address, timezone=timezone, created_by_id=created_by_id
        )
        session.add(place)
        session.flush()  # assign the id and make it findable (check_places does a get)
        place_id = place.id

    if cache is not None:
        cache[key] = place_id
    return place_id
