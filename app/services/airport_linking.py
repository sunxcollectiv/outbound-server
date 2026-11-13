"""Attach a Google place to a seeded airport — only when the match is certain.

Seeded airports (app.seeds.airports) come from OurAirports, which has no street
address, photos or Google id. This looks an airport up with Google Text Search
and, when one result is confidently the same airport, links it: sets
`google_place_id`, caches the result as `google_data` (so the cover photo works
straight away) and swaps the seed's "City, Country" placeholder for Google's
real address.

"Confident" is deliberately strict, because a wrong link is worse than none —
it would show another airport's photo and address. A result is accepted only if
it is airport-typed, sits within a few km of our coordinates, and either its
name agrees with ours or it is practically on top of our coordinates. And then
only if it is the *single* such result — or one of several listings of the same
airport at the same spot, in which case Google's top-ranked one is taken.
Anything else is left unlinked, with the reason reported, for a person to settle.

When the matched Google place already belongs to another of our places — an
airport someone added through the app before the seed existed — the two are the
same airport, so the seeded row is merged into that older one rather than left
as a duplicate (see `_merge_into`). `merge_places` is the general form, for
folding any duplicate place into the one to keep.
"""
from __future__ import annotations

import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Place
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesClient,
    apply_google_place,
)

# Google place types that mean "somewhere scheduled flights leave from".
AIRPORT_TYPES = frozenset({"airport", "international_airport", "airstrip", "heliport"})

# A candidate further than this from our coordinates is a different airport.
# Generous because OurAirports marks the airfield's reference point and Google
# pins the terminal, which are kilometres apart at the largest airports.
MAX_DISTANCE_KM = 5.0
# Close enough that the place is this airport whatever it is called (a local
# name, a renaming) — provided nothing else airport-typed is nearby.
SAME_SPOT_KM = 1.5
# Share of the shorter name's distinctive words the two names must have in common.
MIN_NAME_OVERLAP = 0.5

# Words that say "this is an airport" in the languages airport names come in;
# they carry no identity, so they are ignored when comparing names.
_GENERIC_WORDS = frozenset(
    """
    airport airfield airstrip aerodrome aerodromo aeroport aeroporto aeropuerto
    aeroporti flughafen lufthavn lufthamn flygplats luchthaven lentoasema
    international internacional internazionale intl domestic regional municipal
    field heliport seaplane base terminal air
    de del di da do la le el the of and
    """.split()
)

@dataclass(frozen=True)
class LinkResult:
    """What happened for one airport. `linked` says whether a Google place was
    attached; `reason` says why, in words fit for a log line."""

    linked: bool
    reason: str
    google_place_id: str | None = None
    google_name: str | None = None
    # Set when the seeded airport was folded into an existing place: that
    # place's id. The seeded row no longer exists.
    merged_into: uuid.UUID | None = None


def _words(name: str) -> set[str]:
    """The distinctive words of an airport name: folded to plain lowercase
    ASCII-ish tokens, minus the generic ones."""
    decomposed = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return {word for word in re.findall(r"[^\W_]+", folded) if word not in _GENERIC_WORDS}


def names_agree(ours: str, theirs: str, code: str | None = None) -> bool:
    """True when two airport names plainly refer to the same airport: they share
    enough distinctive words, or Google's name carries our code ("... (LAX)")."""
    their_words = _words(theirs)
    if code and code.casefold() in their_words:
        return True
    our_words = _words(ours)
    if not our_words or not their_words:
        return False
    shared = our_words & their_words
    return len(shared) / min(len(our_words), len(their_words)) >= MIN_NAME_OVERLAP


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _is_airport(candidate: dict[str, Any]) -> bool:
    types = set(candidate.get("types") or [])
    if candidate.get("primaryType"):
        types.add(candidate["primaryType"])
    return bool(types & AIRPORT_TYPES)


def _name(candidate: dict[str, Any]) -> str:
    return (candidate.get("displayName") or {}).get("text") or ""


def _apart_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    return distance_km(
        a["location"]["latitude"],
        a["location"]["longitude"],
        b["location"]["latitude"],
        b["location"]["longitude"],
    )


def choose_match(place: Place, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The one candidate that is confidently this airport, or None, plus the
    reason either way."""
    if place.latitude is None or place.longitude is None:
        return None, "airport has no coordinates to check a match against"

    nearby: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        location = candidate.get("location") or {}
        if not candidate.get("id") or not _is_airport(candidate) or "latitude" not in location:
            continue
        distance = distance_km(
            place.latitude, place.longitude, location["latitude"], location["longitude"]
        )
        if distance <= MAX_DISTANCE_KM:
            nearby.append((distance, candidate))

    if not nearby:
        return None, f"no airport within {MAX_DISTANCE_KM:g} km among {len(candidates)} result(s)"

    named = [
        (distance, candidate)
        for distance, candidate in nearby
        if names_agree(place.name or "", _name(candidate), place.code)
    ]
    if len(named) == 1:
        distance, candidate = named[0]
        return candidate, f"name matches, {distance:.1f} km away"
    if len(named) > 1:
        # Google often lists one airport several times (its operator, its code,
        # its navigation beacon). When every fitting result is at the same
        # spot they are that one airport, so take the one Google ranks first.
        _, first = named[0]
        if all(_apart_km(first, other) <= SAME_SPOT_KM for _, other in named[1:]):
            distance = named[0][0]
            return first, f"top of {len(named)} listings at the same spot, {distance:.1f} km away"
        return None, "ambiguous: " + " / ".join(_name(c) for _, c in named)

    # No name agreement. Accept only a lone airport sitting on our coordinates.
    if len(nearby) == 1 and nearby[0][0] <= SAME_SPOT_KM:
        distance, candidate = nearby[0]
        return candidate, f"only airport at the spot, {distance:.1f} km away (name differs)"
    return None, "name differs: " + " / ".join(_name(c) for _, c in nearby)


def merge_places(session: Session, duplicate: Place, keeper: Place) -> None:
    """Fold one place into another that is the same real-world spot. Whatever
    pointed at the duplicate (trip items, days, transit segments) is repointed
    to the keeper, the keeper takes any field it is missing from the duplicate,
    and the duplicate is deleted. The keeper's own values always win."""
    for table in Base.metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table.name == Place.__tablename__:
                column = foreign_key.parent
                session.execute(
                    update(table).where(column == duplicate.id).values({column: keeper.id})
                )

    for field in ("name", "address", "latitude", "longitude", "timezone", "area_id"):
        if getattr(keeper, field) is None:
            setattr(keeper, field, getattr(duplicate, field))
    session.delete(duplicate)
    session.flush()


def _merge_into(session: Session, seeded: Place, keeper: Place) -> None:
    """Fold a seeded airport into the existing place that already holds its
    Google id. The keeper wins — it has the Google data and is what trips
    already point at — and takes the airport category and code from the seeded
    row, which is deleted."""
    category, code = seeded.category, seeded.code
    # The seeded row must be gone before the keeper takes its code: the pair
    # (category, code) is unique.
    merge_places(session, seeded, keeper)
    keeper.category = category
    keeper.code = code
    session.flush()


def link_airport(session: Session, client: GooglePlacesClient, place: Place) -> LinkResult:
    """Search Google for this airport and link it if the match is confident.
    Changes are left uncommitted on the session; the caller commits. After a
    merge (`merged_into` set) `place` has been deleted — don't touch it again.
    Raises GooglePlacesError if the search itself fails."""
    candidates = client.search_text(
        " ".join(part for part in (place.name, place.code) if part),
        latitude=place.latitude,
        longitude=place.longitude,
        included_type="airport",
    )
    match, reason = choose_match(place, candidates)
    if match is None:
        return LinkResult(False, reason)

    keeper = session.scalar(
        select(Place).where(Place.google_place_id == match["id"], Place.id != place.id)
    )
    if keeper is not None:
        # We already have this Google place. Same airport, so merge — unless the
        # existing place carries a different code, which a person should look at.
        if keeper.code is not None and (keeper.category, keeper.code) != (place.category, place.code):
            return LinkResult(
                False,
                f"Google place belongs to place {keeper.id}, which has code {keeper.code}",
                match["id"],
                _name(match),
            )
        _merge_into(session, place, keeper)
        return LinkResult(
            True,
            f"merged into existing place {keeper.id}; {reason}",
            match["id"],
            _name(match),
            merged_into=keeper.id,
        )

    place.google_place_id = match["id"]
    # Caches the result, fills anything still empty and swaps the seed's
    # placeholder address for the real one; never overwrites our own data.
    apply_google_place(place, match)
    return LinkResult(True, reason, match["id"], _name(match))


def fill_airport_details(client: GooglePlacesClient, place: Place) -> LinkResult:
    """Fetch Place Details for an airport that has a Google id but no Google data
    yet (a new server, seeded with ids only) and apply them: the cache, the
    cover photo, and the real address in place of the seed's placeholder.
    Changes are left uncommitted. A Google id Google no longer knows is
    reported, not fixed: the airport keeps it for a person to re-link. Raises
    GooglePlacesError if the request itself fails."""
    try:
        details = client.fetch_place(place.google_place_id)
    except GooglePlaceNotFound:
        return LinkResult(False, "Google no longer knows this place id", place.google_place_id)
    apply_google_place(place, details)
    return LinkResult(True, "details fetched", place.google_place_id, _name(details))
