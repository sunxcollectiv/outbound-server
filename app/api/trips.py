import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    CurrentUser,
    OrganizerTrip,
    OwnedTrip,
    SessionDep,
    ViewableTrip,
    is_organizer,
)
from app.api.trip_items import check_places, viewer, with_item_details
from app.models import (
    Area,
    Place,
    Trip,
    TripDay,
    TripItemParticipant,
    TripMember,
    TripRole,
    User,
)
from app.schemas import (
    Itinerary,
    TripCreate,
    TripDayRead,
    TripDayUpdate,
    TripMemberCreate,
    TripMemberRead,
    TripRead,
    TripUpdate,
)
from app.services.item_visibility import visible_items
from app.services.itinerary import build_itinerary
from app.services.trip_days import sync_trip_days

router = APIRouter(prefix="/trips", tags=["trips"])

NOT_NULLABLE = ("title", "start_date", "end_date")


def _clean_area_ids(session: SessionDep, area_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Dedupe (keeping order) and check every id names a real area. 404s on any
    unknown id rather than silently storing a dangling reference."""
    unique = list(dict.fromkeys(area_ids))
    if not unique:
        return []
    found = set(session.scalars(select(Area.id).where(Area.id.in_(unique))))
    missing = [str(i) for i in unique if i not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Area(s) not found: {', '.join(missing)}")
    return unique


def _remove_member(session: SessionDep, trip: Trip, member: TripMember) -> None:
    """They come off every item they were a participant on. Items they added
    stay. The FK would cascade the rows, but removing them through each item
    keeps any item already loaded in the session in step."""
    answers = select(TripItemParticipant).where(TripItemParticipant.member_id == member.id)
    for row in session.scalars(answers.options(selectinload(TripItemParticipant.item))):
        row.item.participants.remove(row)  # delete-orphan
    trip.members.remove(member)  # delete-orphan


def _new_member(session: SessionDep, trip: Trip, entry: TripMemberCreate) -> TripMember:
    """A member row for `entry`, linked to the account with that email if any."""
    member = TripMember(
        email=entry.email,
        role=entry.role,
        user_id=session.scalar(select(User.id).where(User.email == entry.email)),
    )
    trip.members.append(member)
    return member


def _set_members(
    session: SessionDep, trip: Trip, entries: list[TripMemberCreate], *, replace: bool
) -> None:
    """Bring the roster in line with `entries`, matched by email: listed people
    keep their row with the role given, new ones are added. With `replace`,
    anyone not listed is removed. The owner is never removed or demoted."""
    current = {member.email: member for member in trip.members}
    for entry in entries:
        member = current.get(entry.email)
        if member is None:
            _new_member(session, trip, entry)
            continue
        if member.user_id == trip.owner_id and entry.role is not TripRole.ORGANIZER:
            raise HTTPException(status.HTTP_409_CONFLICT, "The owner is always an organizer")
        member.role = entry.role
    if replace:
        listed = {entry.email for entry in entries}
        for member in list(trip.members):
            if member.email not in listed and member.user_id != trip.owner_id:
                _remove_member(session, trip, member)


def attach_areas(session: SessionDep, trips: list[Trip]) -> None:
    """Resolve each trip's area_ids into `Area` objects (in stored order) for
    the response, and drop any id that no longer matches an area — e.g. the
    area was deleted — so stale ids never surface as blanks. A prune is written
    back only when one actually happened, so an ordinary read stays read-only.
    Batched into one lookup across all the trips given.

    This never loads or fetches photos — that's the second step. Each trip
    reports the ids of its areas with no cover yet (`areas_missing_photos`, i.e.
    no default_photo_url) so the client can render now and call
    POST /areas/refresh-photos to fill the covers."""
    wanted = {area_id for trip in trips for area_id in trip.area_ids}
    areas = (
        {
            area.id: area
            for area in session.scalars(
                select(Area).options(selectinload(Area.parent)).where(Area.id.in_(wanted))
            )
        }
        if wanted
        else {}
    )

    pruned = False
    for trip in trips:
        resolved = [areas[area_id] for area_id in trip.area_ids if area_id in areas]
        kept = [area.id for area in resolved]
        if kept != trip.area_ids:
            trip.area_ids = kept
            pruned = True
        trip.areas = resolved  # transient attribute read by TripRead
        trip.areas_missing_photos = [
            area.id for area in resolved if area.default_photo_url is None
        ]

    if pruned:
        session.commit()


@router.post("", response_model=TripRead, status_code=status.HTTP_201_CREATED)
def create_trip(payload: TripCreate, user: CurrentUser, session: SessionDep) -> Trip:
    """The creator becomes the owner and gets an organizer membership row, so
    trip_members is the full headcount and roles live in one place; anyone in
    `members` is added alongside. A day row is made for every date, seeded
    with the timezone if one was given."""
    values = payload.model_dump()
    seed_timezone = values.pop("timezone")
    del values["members"]
    values["area_ids"] = _clean_area_ids(session, values["area_ids"])
    trip = Trip(**values, owner_id=user.id)
    trip.members.append(TripMember(user_id=user.id, email=user.email, role=TripRole.ORGANIZER))
    _set_members(session, trip, payload.members, replace=False)
    sync_trip_days(trip, seed_timezone)
    session.add(trip)
    session.commit()
    attach_areas(session, [trip])
    return trip


@router.get("", response_model=list[TripRead])
def list_trips(user: CurrentUser, session: SessionDep) -> list[Trip]:
    """Trips the caller is on, as owner or member."""
    member_of = select(TripMember.trip_id).where(TripMember.user_id == user.id)
    query = (
        select(Trip)
        .where(Trip.id.in_(member_of))
        .options(selectinload(Trip.members).selectinload(TripMember.user))
        .order_by(Trip.start_date, Trip.id)
    )
    trips = list(session.scalars(query))
    attach_areas(session, trips)
    return trips


@router.get("/{trip_id}", response_model=TripRead)
def read_trip(trip: ViewableTrip, session: SessionDep) -> Trip:
    attach_areas(session, [trip])
    return trip


@router.patch("/{trip_id}", response_model=TripRead)
def update_trip(payload: TripUpdate, trip: OrganizerTrip, session: SessionDep) -> Trip:
    changes = payload.model_dump(exclude_unset=True)
    area_ids = changes.pop("area_ids", None)
    changes.pop("members", None)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be null")
    start = changes.get("start_date", trip.start_date)
    end = changes.get("end_date", trip.end_date)
    if end < start:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "end_date must be on or after start_date"
        )
    for field, value in changes.items():
        setattr(trip, field, value)
    if area_ids is not None:
        trip.area_ids = _clean_area_ids(session, area_ids)
    if payload.members is not None:
        _set_members(session, trip, payload.members, replace=True)
    if "start_date" in changes or "end_date" in changes:
        sync_trip_days(trip)
    session.commit()
    attach_areas(session, [trip])
    return trip


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip: OwnedTrip, session: SessionDep) -> None:
    session.delete(trip)
    session.commit()


@router.get("/{trip_id}/itinerary", response_model=Itinerary)
def read_itinerary(trip: ViewableTrip, user: CurrentUser, session: SessionDep) -> Itinerary:
    """Everyone's plans in one view, each item as the caller gets to see it:
    private items only if they're on them, closed ones cut down unless they are."""
    member = viewer(session, trip, user)
    items = session.scalars(with_item_details(visible_items(trip, member)))
    return build_itinerary(trip, list(items), member.id)


# --- days ---


def _days_query(trip: Trip):
    return (
        select(TripDay)
        .where(TripDay.trip_id == trip.id)
        .options(selectinload(TripDay.place).selectinload(Place.area))
        .order_by(TripDay.date)
    )


@router.get("/{trip_id}/days", response_model=list[TripDayRead])
def list_days(trip: ViewableTrip, session: SessionDep) -> list[TripDay]:
    """One entry per date of the trip, in order."""
    return list(session.scalars(_days_query(trip)))


@router.patch("/{trip_id}/days/{day}", response_model=TripDayRead)
def update_day(
    day: date, payload: TripDayUpdate, trip: OrganizerTrip, session: SessionDep
) -> TripDay:
    """Name a day, say which zones it runs in, or point it at a place. Days
    themselves come and go with the trip's dates."""
    trip_day = session.get(TripDay, (trip.id, day))
    if trip_day is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That date isn't on the trip")
    changes = payload.model_dump(exclude_unset=True)
    if "timezones" in changes and changes["timezones"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "timezones cannot be null")
    check_places(session, changes)
    for field, value in changes.items():
        setattr(trip_day, field, value)
    session.commit()
    session.refresh(trip_day)
    return trip_day


# --- members ---


@router.get("/{trip_id}/members", response_model=list[TripMemberRead])
def list_members(trip: ViewableTrip, session: SessionDep) -> list[TripMember]:
    """Everyone on the trip, owner included, with or without an account."""
    query = (
        select(TripMember)
        .where(TripMember.trip_id == trip.id)
        .options(selectinload(TripMember.user))
        .order_by(TripMember.created_at, TripMember.id)
    )
    return list(session.scalars(query))


@router.post(
    "/{trip_id}/members", response_model=TripMemberRead, status_code=status.HTTP_201_CREATED
)
def add_member(payload: TripMemberCreate, trip: OrganizerTrip, session: SessionDep) -> TripMember:
    """Adds one person by email. Linked to their account now if one exists,
    otherwise when they register; the row stays on the roster either way."""
    if any(member.email == payload.email for member in trip.members):
        raise HTTPException(status.HTTP_409_CONFLICT, "That email is already on the trip")
    member = _new_member(session, trip, payload)
    session.commit()
    session.refresh(member)
    return member


@router.delete("/{trip_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    member_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    """Organizers can remove anyone but the owner; a member can remove themself (leave).

    Their personal items and RSVPs go with them; group items they posted stay."""
    member = next((m for m in trip.members if m.id == member_id), None)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if member.user_id != user.id and not is_organizer(session, trip, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can remove others")
    if member.user_id == trip.owner_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The owner can't be removed from their trip")
    _remove_member(session, trip, member)
    session.commit()
