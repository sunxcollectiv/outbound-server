"""What counts as editing an item's booking, for `TripItem.edited_at`, plus the
plain-value views of an item's legs, lodging details and rooms that both this
and the change notifications (app.services.item_notifications) compare.

An imported item starts out exactly as its booking said; `edited_at` marks the
first (and each later) time a person changed it, so the client can say "edited
since import". Only the booking itself counts: kind, title, times and zones,
places, references, transit mode and legs, lodging details and rooms. Who's on
the item (or in which room), its visibility, requirement, notes, tags and
image are the user's own arrangement on top of the booking, so changing them
isn't editing it.

Each view copies values out, so take it before changing anything: later
in-place changes to the item (or its lodging/rooms/legs) don't leak into it.
"""
from typing import Any

from app.models import TripItem, TripItemLodging, TripItemLodgingRoom, TripItemTransitSegment


def legs_state(item: TripItem) -> tuple[Any, ...]:
    return tuple(
        tuple(getattr(leg, field) for field in TripItemTransitSegment.FIELDS)
        for leg in item.transit_segments
    )


def lodging_state(item: TripItem) -> tuple[Any, ...] | None:
    stay = item.lodging
    return tuple(getattr(stay, field) for field in TripItemLodging.FIELDS) if stay else None


def rooms_state(item: TripItem, *, occupants: bool = False) -> tuple[Any, ...]:
    """Each room's id, position and fields; with `occupants`, who's in it too."""
    return tuple(
        (
            room.id,
            room.position,
            *(getattr(room, field) for field in TripItemLodgingRoom.FIELDS),
            *((frozenset(room.occupant_ids),) if occupants else ()),
        )
        for room in item.rooms
    )


def booking_state(item: TripItem) -> tuple[Any, ...]:
    """The item's booking content, to compare before and after an edit."""
    return (
        item.kind,
        item.title,
        item.starts_at,
        item.start_timezone,
        item.ends_at,
        item.end_timezone,
        item.place_id,
        item.end_place_id,
        item.confirmation_number,
        item.booking_number,
        item.transit_mode,
        legs_state(item),
        lodging_state(item),
        rooms_state(item),  # the rooms themselves, not who's in them
    )
