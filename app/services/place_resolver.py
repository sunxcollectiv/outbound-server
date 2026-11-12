"""Resolve an extracted place descriptor (name/address/city/IATA) to a `Place`.

Two ways to match, tried in order:

1. **By code**, when the caller says what the descriptor's code means (today:
   `code_category="airport"` for a flight, where `iata_code` is the IATA code).
   A code identifies one place within its category, so this is an exact lookup
   — "LAX" always resolves to the one LAX airport, normally the seeded one
   (app.seeds.airports). An airport we don't have yet is created *with* its
   code, so the next booking finds it instead of adding another.
2. **By name AND address**, for everything else: reuse an existing place only
   on a solid match of both (present and equal, normalized); anything short of
   that creates a new `Place` with `google_place_id` null. We'd rather keep a
   faithful copy of what the itinerary said than force it onto a look-alike —
   so duplicates are expected and fine here (name-only matching would wrongly
   merge same-named places in different cities). A real similarity score (e.g.
   pg_trgm), city→area linking, duplicate merging, and platform-id attachment
   are pre-launch work.

The code is only trusted when the caller vouches for it: the extractor fills
`iata_code` for stations too, and a rail station code must not land on an
airport.

Reusable on its own; the mail-import stage calls it while building items, and a
future admin/re-resolve flow can use the same function. Pass a per-call `cache`
so the same place appearing twice in one booking (e.g. a layover airport that is
one leg's arrival and the next's departure) resolves to a single `Place`.
"""
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Place


def _norm(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


PlaceCache = dict[tuple[str | None, ...], uuid.UUID]

# What a usable code looks like, per category. IATA airport codes are three
# letters/digits; anything else in the field is a misread, so it is ignored.
_CODE_FORMATS = {"airport": re.compile(r"[A-Z0-9]{3}")}


def _clean_code(value: Any, category: str | None) -> str | None:
    pattern = _CODE_FORMATS.get(category or "")
    if pattern is None or not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if pattern.fullmatch(code) else None


def _resolve_by_code(
    session: Session,
    category: str,
    code: str,
    *,
    name: str | None,
    address: str | None,
    timezone: str | None,
    created_by_id: uuid.UUID | None,
) -> uuid.UUID:
    """The place with this category and code, creating it (with the code) when
    we don't have one."""
    lookup = select(Place.id).where(Place.category == category, Place.code == code)
    place_id = session.scalars(lookup).first()
    if place_id is not None:
        return place_id
    place = Place(
        name=name or code,
        category=category,
        code=code,
        address=address,
        timezone=timezone,
        created_by_id=created_by_id,
    )
    try:
        # A savepoint, so losing a race for the same new code (the pair is
        # unique) doesn't undo the import around it.
        with session.begin_nested():
            session.add(place)
    except IntegrityError:
        return session.scalars(lookup).one()
    return place.id


def resolve_place(
    session: Session,
    descriptor: dict[str, Any] | None,
    *,
    timezone: str | None = None,
    created_by_id: uuid.UUID | None = None,
    cache: PlaceCache | None = None,
    code_category: str | None = None,
) -> uuid.UUID | None:
    """Return the id of the Place this descriptor refers to, creating one if no
    existing place matches. None when there's nothing to resolve (no name,
    address or usable code). `timezone` seeds a newly-created place (e.g. the
    leg's zone for an airport). `code_category` says what the descriptor's
    `iata_code` identifies — pass "airport" for a flight — and turns on the
    match by code; leave it None when the code can't be trusted to be one."""
    if not descriptor:
        return None
    name = descriptor.get("name") or descriptor.get("iata_code")
    address = descriptor.get("address")

    code = _clean_code(descriptor.get("iata_code"), code_category)
    if code is not None:
        code_key = ("code", code_category, code)
        if cache is not None and code_key in cache:
            return cache[code_key]
        place_id = _resolve_by_code(
            session,
            code_category,
            code,
            name=descriptor.get("name"),
            address=address,
            timezone=timezone,
            created_by_id=created_by_id,
        )
        if cache is not None:
            cache[code_key] = place_id
        return place_id

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
