"""Reading a trip together with the others in its group.

A person sees the trips of a group they're on as one journey: one card in
their trip list, one merged itinerary. Which of their trips leads, and which
trip owns a date two of them share, both go by the parent links, read across
the whole group (trips they aren't on included), so a chain A → B → C still
puts A above C for someone on A and C only.
"""

import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Trip, TripMember, User

# trip id → the trip it extends (None for a root, or once the parent is gone)
Links = dict[uuid.UUID, uuid.UUID | None]


def group_links(session: Session, group_ids: Iterable[uuid.UUID | None]) -> Links:
    """The parent link of every trip in these groups, in one query."""
    wanted = {group_id for group_id in group_ids if group_id is not None}
    if not wanted:
        return {}
    rows = session.execute(
        select(Trip.id, Trip.extends_trip_id).where(Trip.group_id.in_(wanted))
    )
    return {trip_id: parent_id for trip_id, parent_id in rows}


def _ancestors(trip_id: uuid.UUID, links: Links) -> list[uuid.UUID]:
    """The trips `trip_id` was extended from, nearest first."""
    found = []
    parent = links.get(trip_id)
    while parent is not None and parent not in found:
        found.append(parent)
        parent = links.get(parent)
    return found


def depth(trip_id: uuid.UUID, links: Links) -> int:
    """Hops up to a trip that extends nothing. A trip whose parent was
    deleted counts as a root."""
    return len(_ancestors(trip_id, links))


def _by_date(trip: Trip) -> tuple[date, object]:
    return trip.start_date, trip.created_at


def split_primary(trips: list[Trip], links: Links) -> tuple[Trip, list[Trip]]:
    """The trip that leads a person's trips in one group, and the rest by date.
    It's the one none of the others descend from; if links were broken by a
    deletion and several qualify, the earliest."""
    ids = {trip.id for trip in trips}
    roots = [t for t in trips if not ids.intersection(_ancestors(t.id, links))]
    primary = min(roots or trips, key=_by_date)
    return primary, sorted((t for t in trips if t is not primary), key=_by_date)


def my_cluster(session: Session, trip: Trip, user: User) -> list[Trip]:
    """`trip` and every other trip in its group that `user` is on, by date."""
    if trip.group_id is None:
        return [trip]
    member_of = select(TripMember.trip_id).where(TripMember.user_id == user.id)
    query = (
        select(Trip)
        .where(Trip.group_id == trip.group_id, Trip.id.in_(member_of))
        .options(selectinload(Trip.members).selectinload(TripMember.user))
    )
    return sorted(session.scalars(query), key=_by_date)


def day_owner(day: date, trips: list[Trip], links: Links) -> Trip | None:
    """Which of `trips` a date belongs to. Where two share it, the parent
    wins (the trip fewer hops from the root), then the earlier trip. None if
    none of them covers it."""
    covering = [trip for trip in trips if trip.start_date <= day <= trip.end_date]
    if not covering:
        return None
    return min(covering, key=lambda trip: (depth(trip.id, links), *_by_date(trip)))
