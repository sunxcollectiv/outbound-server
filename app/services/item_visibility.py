"""Who sees what of a trip item.

Participants always see an item in full. Everyone else on the trip goes by its
visibility: a private item doesn't exist for them, an open one is shown in
full, and a closed one is cut down to its kind, title and times (`redacted`).
Every path that returns items to a caller goes through here, the itinerary
included, so a closed item's details never leak through a side door.
"""

import uuid

from sqlalchemy import Select, or_, select

from app.models import Trip, TripItem, TripItemParticipant, TripItemVisibility, TripMember
from app.schemas import TripItemRead

# What a non-participant loses on a closed item.
_REDACTED: dict[str, object] = {
    "redacted": True,
    "requirement": None,
    "notes": None,
    "confirmation_number": None,
    "transit_mode": None,
    "tags": [],
    "image_url": None,
    "place": None,
    "end_place": None,
    "segments": [],
    "created_by": None,
    "participants": [],
}


def visible_items(trip: Trip, member: TripMember) -> Select[tuple[TripItem]]:
    """Items on the trip that `member` may see at all: anything not private,
    and private items they're a participant on."""
    theirs = select(TripItemParticipant.item_id).where(TripItemParticipant.member_id == member.id)
    return (
        select(TripItem)
        .where(TripItem.trip_id == trip.id)
        .where(
            or_(TripItem.visibility != TripItemVisibility.PRIVATE, TripItem.id.in_(theirs))
        )
    )


def is_redacted(item: TripItem, member_id: uuid.UUID) -> bool:
    return item.visibility is TripItemVisibility.CLOSED and not any(
        row.member_id == member_id for row in item.participants
    )


def present_item(item: TripItem, member_id: uuid.UUID) -> TripItemRead:
    """The item as `member_id` gets to see it."""
    read = TripItemRead.model_validate(item)
    return read.model_copy(update=_REDACTED) if is_redacted(item, member_id) else read
