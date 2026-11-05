"""What counts as editing an item's booking, for `TripItem.edited_at`.

An imported item starts out exactly as its booking said; `edited_at` marks the
first (and each later) time a person changed it, so the client can say "edited
since import". Only the booking itself counts: kind, title, times and zones,
places, references, transit mode and legs, lodging details and rooms. Who's on
the item (or in which room), its visibility, requirement, notes, tags and
image are the user's own arrangement on top of the booking, so changing them
isn't editing it.
"""
from typing import Any

from app.models import TripItem


def booking_state(item: TripItem) -> tuple[Any, ...]:
    """The item's booking content as plain values, to compare before and after
    an edit. Take it before changing anything: it copies, so later in-place
    changes to the item (or its lodging/rooms/legs) don't leak into it."""
    stay = item.lodging
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
        tuple(
            (
                leg.position,
                leg.starts_at,
                leg.start_timezone,
                leg.ends_at,
                leg.end_timezone,
                leg.place_id,
                leg.end_place_id,
                leg.carrier,
                leg.flight_number,
                leg.transit_mode,
                leg.confirmation_number,
            )
            for leg in item.transit_segments
        ),
        (stay.property_type, stay.phone, stay.free_cancellation_until) if stay else None,
        # The rooms themselves, not who's in them.
        tuple(
            (
                room.id,
                room.position,
                room.label,
                room.room_number,
                room.room_type,
                room.guests,
                room.confirmation_number,
            )
            for room in item.rooms
        ),
    )
