import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.deps import (
    CurrentUser,
    OrganizerTrip,
    OwnedTrip,
    SessionDep,
    ViewableTrip,
    is_organizer,
)
from app.api.trip_items import check_places, visible_items, with_item_details
from app.models import (
    Place,
    Trip,
    TripDay,
    TripItem,
    TripItemParticipant,
    TripItemScope,
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
from app.services.itinerary import build_itinerary
from app.services.trip_days import sync_trip_days

router = APIRouter(prefix="/trips", tags=["trips"])

NOT_NULLABLE = ("title", "start_date", "end_date")


@router.post("", response_model=TripRead, status_code=status.HTTP_201_CREATED)
def create_trip(payload: TripCreate, user: CurrentUser, session: SessionDep) -> Trip:
    """The creator becomes the owner and gets an organizer membership row, so
    trip_members is the full headcount and roles live in one place. A day row
    is made for every date, seeded with the timezone if one was given."""
    values = payload.model_dump()
    seed_timezone = values.pop("timezone")
    trip = Trip(**values, owner_id=user.id)
    trip.members.append(TripMember(user_id=user.id, role=TripRole.ORGANIZER))
    sync_trip_days(trip, seed_timezone)
    session.add(trip)
    session.commit()
    return trip


@router.get("", response_model=list[TripRead])
def list_trips(user: CurrentUser, session: SessionDep) -> list[Trip]:
    """Trips the caller is on, as owner or member."""
    member_of = select(TripMember.trip_id).where(TripMember.user_id == user.id)
    query = select(Trip).where(Trip.id.in_(member_of)).order_by(Trip.start_date, Trip.id)
    return list(session.scalars(query))


@router.get("/{trip_id}", response_model=TripRead)
def read_trip(trip: ViewableTrip) -> Trip:
    return trip


@router.patch("/{trip_id}", response_model=TripRead)
def update_trip(payload: TripUpdate, trip: OrganizerTrip, session: SessionDep) -> Trip:
    changes = payload.model_dump(exclude_unset=True)
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
    if "start_date" in changes or "end_date" in changes:
        sync_trip_days(trip)
    session.commit()
    return trip


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip: OwnedTrip, session: SessionDep) -> None:
    session.delete(trip)
    session.commit()


@router.get("/{trip_id}/itinerary", response_model=Itinerary)
def read_itinerary(trip: ViewableTrip, user: CurrentUser, session: SessionDep) -> Itinerary:
    """Everyone's plans in one view: group items plus every member's shared
    items, each tagged with who added it. Private items show only to their creator."""
    items = session.scalars(with_item_details(visible_items(trip, user)))
    return build_itinerary(trip, list(items))


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
    """Everyone on the trip, owner included."""
    query = (
        select(TripMember)
        .where(TripMember.trip_id == trip.id)
        .options(selectinload(TripMember.user))
        .order_by(TripMember.created_at, TripMember.user_id)
    )
    return list(session.scalars(query))


@router.post(
    "/{trip_id}/members", response_model=TripMemberRead, status_code=status.HTTP_201_CREATED
)
def add_member(payload: TripMemberCreate, trip: OrganizerTrip, session: SessionDep) -> TripMember:
    if session.get(User, payload.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if session.get(TripMember, (trip.id, payload.user_id)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is already on the trip")
    member = TripMember(trip_id=trip.id, user_id=payload.user_id, role=payload.role)
    session.add(member)
    session.commit()
    return member


@router.delete("/{trip_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    user_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    """Organizers can remove anyone but the owner; a member can remove themself (leave).

    Their personal items and RSVPs go with them; group items they posted stay."""
    if user.id != user_id and not is_organizer(session, trip, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can remove others")
    member = session.get(TripMember, (trip.id, user_id))
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if user_id == trip.owner_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The owner can't be removed from their trip")

    trip_item_ids = select(TripItem.id).where(TripItem.trip_id == trip.id)
    session.execute(
        delete(TripItemParticipant).where(
            TripItemParticipant.user_id == user_id,
            TripItemParticipant.item_id.in_(trip_item_ids),
        )
    )
    session.execute(
        delete(TripItem).where(
            TripItem.trip_id == trip.id,
            TripItem.scope == TripItemScope.PERSONAL,
            TripItem.created_by_id == user_id,
        )
    )
    session.delete(member)
    session.commit()
