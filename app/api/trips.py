import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, EditableTrip, OwnedTrip, SessionDep, ViewableTrip
from app.models import Trip, TripItem, TripMember, User
from app.schemas import (
    Itinerary,
    TripCreate,
    TripMemberCreate,
    TripMemberRead,
    TripRead,
    TripUpdate,
)
from app.services.itinerary import build_itinerary

router = APIRouter(prefix="/trips", tags=["trips"])

NOT_NULLABLE = ("title", "start_date", "end_date", "timezone")


@router.post("", response_model=TripRead, status_code=status.HTTP_201_CREATED)
def create_trip(payload: TripCreate, user: CurrentUser, session: SessionDep) -> Trip:
    trip = Trip(**payload.model_dump(), owner_id=user.id)
    session.add(trip)
    session.commit()
    return trip


@router.get("", response_model=list[TripRead])
def list_trips(user: CurrentUser, session: SessionDep) -> list[Trip]:
    """Trips the caller owns or has been added to."""
    member_of = select(TripMember.trip_id).where(TripMember.user_id == user.id)
    query = (
        select(Trip)
        .where(or_(Trip.owner_id == user.id, Trip.id.in_(member_of)))
        .order_by(Trip.start_date, Trip.id)
    )
    return list(session.scalars(query))


@router.get("/{trip_id}", response_model=TripRead)
def read_trip(trip: ViewableTrip) -> Trip:
    return trip


@router.patch("/{trip_id}", response_model=TripRead)
def update_trip(payload: TripUpdate, trip: EditableTrip, session: SessionDep) -> Trip:
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
    session.commit()
    return trip


@router.delete("/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip: OwnedTrip, session: SessionDep) -> None:
    session.delete(trip)
    session.commit()


@router.get("/{trip_id}/itinerary", response_model=Itinerary)
def read_itinerary(trip: ViewableTrip, session: SessionDep) -> Itinerary:
    items = session.scalars(
        select(TripItem)
        .where(TripItem.trip_id == trip.id)
        .options(
            selectinload(TripItem.location),
            selectinload(TripItem.end_location),
            selectinload(TripItem.traveler),
        )
    )
    return build_itinerary(trip, list(items))


# --- members ---


@router.get("/{trip_id}/members", response_model=list[TripMemberRead])
def list_members(trip: ViewableTrip, session: SessionDep) -> list[TripMember]:
    """Companions only; the owner is trips.owner_id."""
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
def add_member(payload: TripMemberCreate, trip: OwnedTrip, session: SessionDep) -> TripMember:
    if payload.user_id == trip.owner_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The owner is already on the trip")
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
    """The owner can remove anyone; a member can remove themself (leave)."""
    if user.id not in (trip.owner_id, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the trip owner can remove others")
    member = session.get(TripMember, (trip.id, user_id))
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    session.delete(member)
    session.commit()
