"""Telling a trip's members what an organizer changed.

The organizer opts in per edit (`notify_members` on PATCH /trips/{id}); this
works out what actually changed and writes the notifications. Everyone on the
trip with an account hears about it except whoever made the change; people the
edit added get "added you" instead, and people it removed hear nothing.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import NotificationCategory, Trip, User
from app.services.notifications import notify, trip_audience

# How each change reads in a sentence, in the order they're listed.
_LABELS = {
    "title": "name",
    "dates": "dates",
    "description": "description",
    "cover_image_url": "cover photo",
    "area_ids": "destinations",
    "members": "members",
}


@dataclass(frozen=True)
class TripSnapshot:
    """The parts of a trip an edit can change, to compare before and after."""

    title: str
    description: str | None
    start_date: date
    end_date: date
    cover_image_url: str | None
    area_ids: tuple[uuid.UUID, ...]
    # email -> role, so a role change counts too.
    members: frozenset[tuple[str, str]]
    member_user_ids: frozenset[uuid.UUID]

    @classmethod
    def of(cls, trip: Trip) -> "TripSnapshot":
        return cls(
            title=trip.title,
            description=trip.description,
            start_date=trip.start_date,
            end_date=trip.end_date,
            cover_image_url=trip.cover_image_url,
            area_ids=tuple(trip.area_ids),
            members=frozenset((m.email, m.role.value) for m in trip.members),
            member_user_ids=frozenset(m.user_id for m in trip.members if m.user_id),
        )


def changes_between(before: TripSnapshot, after: TripSnapshot) -> list[str]:
    """What changed, as keys of _LABELS, in that order."""
    changed = {
        "title": before.title != after.title,
        "dates": (before.start_date, before.end_date) != (after.start_date, after.end_date),
        "description": before.description != after.description,
        "cover_image_url": before.cover_image_url != after.cover_image_url,
        "area_ids": before.area_ids != after.area_ids,
        "members": before.members != after.members,
    }
    return [key for key in _LABELS if changed[key]]


def date_range(start: date, end: date) -> str:
    """"Apr 9 – 15, 2026", "Apr 28 – May 3, 2026", "Dec 28, 2026 – Jan 3, 2027"."""
    if start == end:
        return f"{start:%b} {start.day}, {start.year}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start:%b} {start.day} – {end.day}, {end.year}"
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


def _summary(actor: str, changes: list[str], before: TripSnapshot, trip: Trip) -> str:
    if changes == ["title"]:
        return f"{actor} renamed the trip from “{before.title}”"
    if changes == ["dates"]:
        return f"{actor} changed the dates to {date_range(trip.start_date, trip.end_date)}"
    if changes == ["members"]:
        return f"{actor} updated who's going"
    labels = [_LABELS[key] for key in changes]
    if len(labels) == 1:
        return f"{actor} updated the {labels[0]}"
    return f"{actor} updated the {', '.join(labels[:-1])} and {labels[-1]}"


def notify_trip_updated(
    session: Session, trip: Trip, actor: User, before: TripSnapshot
) -> int:
    """Notify the trip's members of what changed since `before` (taken before
    the edit was applied; call after it, before committing). Returns how many
    people were notified; none when nothing actually changed."""
    after = TripSnapshot.of(trip)
    changes = changes_between(before, after)
    if not changes:
        return 0

    audience = set(trip_audience(session, trip))
    added = audience - before.member_user_ids
    common = {
        "category": NotificationCategory.TRIP_ACTIVITY,
        "title": trip.title,
        "actor_id": actor.id,
        "trip_id": trip.id,
    }
    sent = notify(
        session,
        user_ids=audience - added,
        type="trip.updated",
        body=_summary(actor.display_name, changes, before, trip),
        data={"changes": changes},
        **common,
    )
    if added:
        sent += notify(
            session,
            user_ids=added,
            type="trip.member_added",
            body=f"{actor.display_name} added you to the trip",
            **common,
        )
    return sent
