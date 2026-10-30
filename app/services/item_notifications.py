"""Telling a trip's members about an item someone added or changed.

Whoever adds or edits an item opts in per change (`notify_members` on the item
routes). Who hears about it follows what they can see of the item (see
app.services.item_visibility):

- private: its participants only;
- open: everyone on the trip;
- closed: everyone, but people who aren't participants only see its title and
  times, so they're only told when one of those changed, and the message
  mentions nothing else.

Never the person who made the change. Someone the edit makes a participant is
told "added you" instead. Members without an account have no inbox.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    NotificationCategory,
    Trip,
    TripItem,
    TripItemKind,
    TripItemVisibility,
    TripMember,
    User,
)
from app.services.notifications import notify, trip_audience

# How each change reads in a list, in the order they're listed.
_LABELS = {
    "title": "name",
    "time": "time",
    "place": "place",
    "notes": "notes",
    "participants": "participants",
    "details": "details",
}

# What a non-participant can see of a closed item.
_SEEN_WHEN_CLOSED = ("title", "time")

# How an item reads when it has no title and no place to go by.
_KIND_PHRASES = {
    TripItemKind.ACTIVITY: "an activity",
    TripItemKind.LODGING: "a stay",
    TripItemKind.TRANSIT: "a transit item",
}


def _name(
    title: str | None, place_name: str | None, kind: TripItemKind, *, quoted: bool = False
) -> str:
    """How an item reads mid-sentence: its title, else its place's name (pass
    None when the reader can't see the place), else a phrase for its kind.
    `quoted` puts real names in quotes; a kind phrase never is."""
    name = title or place_name
    if not name:
        return _KIND_PHRASES[kind]
    return f"“{name}”" if quoted else name


@dataclass(frozen=True)
class ItemSnapshot:
    """The parts of an item an edit can change, to compare before and after."""

    title: str | None
    time: tuple[Any, ...]
    place: tuple[uuid.UUID | None, ...]
    notes: str | None
    participants: frozenset[uuid.UUID]  # member ids
    details: tuple[Any, ...]
    # To name the item as it was (see _name).
    place_name: str | None
    kind: TripItemKind

    @classmethod
    def of(cls, item: TripItem) -> "ItemSnapshot":
        legs = tuple(
            (
                leg.position,
                leg.starts_at,
                leg.ends_at,
                leg.start_timezone,
                leg.end_timezone,
                leg.place_id,
                leg.end_place_id,
                leg.carrier,
                leg.flight_number,
                leg.transit_mode,
                leg.confirmation_number,
            )
            for leg in item.transit_segments
        )
        return cls(
            title=item.title,
            time=(item.starts_at, item.ends_at, item.start_timezone, item.end_timezone),
            place=(item.place_id, item.end_place_id),
            notes=item.notes,
            participants=frozenset(p.member_id for p in item.participants),
            details=(
                item.kind,
                item.visibility,
                item.requirement,
                item.confirmation_number,
                item.transit_mode,
                tuple(item.tags),
                item.image_url,
                legs,
            ),
            place_name=item.place.name if item.place is not None else None,
            kind=item.kind,
        )


def changes_between(before: ItemSnapshot, after: ItemSnapshot) -> list[str]:
    """What changed, as keys of _LABELS, in that order."""
    return [key for key in _LABELS if getattr(before, key) != getattr(after, key)]


def _and(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def day(moment: datetime) -> str:
    """"Nov 12"."""
    return f"{moment:%b} {moment.day}"


def day_and_time(moment: datetime) -> str:
    """"Nov 12 at 7 PM", "Nov 12 at 7:30 PM". Wall time, as the item states it."""
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{day(moment)} at {hour}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def _item_name(item: TripItem, *, redacted: bool = False, quoted: bool = False) -> str:
    """`redacted` names the item as a non-participant of a closed item sees it:
    without its place."""
    place_name = item.place.name if item.place is not None and not redacted else None
    return _name(item.title, place_name, item.kind, quoted=quoted)


def _summary(
    actor: str, changes: list[str], before: ItemSnapshot, item: TripItem, *, redacted: bool = False
) -> str:
    """`redacted` names the item as a non-participant of a closed item sees it."""
    name = _item_name(item, redacted=redacted)
    if changes == ["title"]:
        old_name = _name(
            before.title, None if redacted else before.place_name, before.kind, quoted=True
        )
        new_name = _item_name(item, redacted=redacted, quoted=True)
        return f"{actor} renamed {old_name} to {new_name}"
    if changes == ["time"]:
        return f"{actor} moved {name} to {day_and_time(item.starts_at)}"
    if changes == ["place"]:
        return f"{actor} changed the place for {name}"
    if changes == ["participants"]:
        return f"{actor} updated who's going to {name}"
    if changes == ["details"]:
        return f"{actor} updated the details of {name}"
    return f"{actor} updated the {_and([_LABELS[key] for key in changes])} of {name}"


def _user_ids(session: Session, member_ids: frozenset[uuid.UUID]) -> set[uuid.UUID]:
    """The accounts behind these trip members (those that have one)."""
    if not member_ids:
        return set()
    return set(
        session.scalars(
            select(TripMember.user_id).where(
                TripMember.id.in_(member_ids), TripMember.user_id.is_not(None)
            )
        )
    )


def _common(trip: Trip, item: TripItem, actor: User) -> dict[str, Any]:
    return {
        "category": NotificationCategory.TRIP_ACTIVITY,
        "title": trip.title,
        "actor_id": actor.id,
        "trip_id": trip.id,
        "trip_item_id": item.id,
    }


def notify_item_created(session: Session, trip: Trip, item: TripItem, actor: User) -> int:
    """Tell the trip about a new item: "Sun added Dinner on Nov 12". Call after
    adding it to the session, before committing. Returns how many people were
    notified. Name and day only, so it suits every visibility, though an
    untitled closed item is named by its kind to those who can't see its place."""
    session.flush()  # the item's id, and its participant rows for the audience
    audience = set(trip_audience(session, trip, item))
    outsiders: set[uuid.UUID] = set()
    if item.visibility is TripItemVisibility.CLOSED:
        outsiders = audience - _user_ids(
            session, frozenset(p.member_id for p in item.participants)
        )
    sent = 0
    for user_ids, name in (
        (audience - outsiders, _item_name(item)),
        (outsiders, _item_name(item, redacted=True)),
    ):
        if user_ids:
            sent += notify(
                session,
                user_ids=user_ids,
                type="trip_item.created",
                body=f"{actor.display_name} added {name} on {day(item.starts_at)}",
                **_common(trip, item, actor),
            )
    return sent


def notify_item_updated(
    session: Session, trip: Trip, item: TripItem, actor: User, before: ItemSnapshot
) -> int:
    """Tell the trip what changed on `item` since `before` (taken before the
    edit; call after applying it, before committing). Returns how many people
    were notified; none when nothing changed."""
    session.flush()
    # The edit sets place_id; reload the place so an untitled item reads by its new one.
    session.expire(item, ["place"])
    after = ItemSnapshot.of(item)
    changes = changes_between(before, after)
    if not changes:
        return 0

    audience = set(trip_audience(session, trip, item))
    participants = _user_ids(session, after.participants)
    added = (participants - _user_ids(session, before.participants)) & audience
    common = _common(trip, item, actor)

    sent = 0
    if added:
        sent += notify(
            session,
            user_ids=added,
            type="trip_item.participant_added",
            body=f"{actor.display_name} added you to {_item_name(item)} on {day(item.starts_at)}",
            **common,
        )

    told_in_full = audience - added
    if item.visibility is TripItemVisibility.CLOSED:
        told_in_full &= participants
        outsiders = audience - participants
        seen = [key for key in changes if key in _SEEN_WHEN_CLOSED]
        if outsiders and seen:
            sent += notify(
                session,
                user_ids=outsiders,
                type="trip_item.updated",
                body=_summary(actor.display_name, seen, before, item, redacted=True),
                data={"changes": seen},
                **common,
            )
    sent += notify(
        session,
        user_ids=told_in_full,
        type="trip_item.updated",
        body=_summary(actor.display_name, changes, before, item),
        data={"changes": changes},
        **common,
    )
    return sent
