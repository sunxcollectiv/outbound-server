import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, EditableTrip, SessionDep, ViewableTrip
from app.models import Location, Trip, TripItem, TripItemKind, TripMember
from app.schemas import TripItemCreate, TripItemRead, TripItemUpdate

router = APIRouter(prefix="/trips/{trip_id}/items", tags=["trip items"])

NOT_NULLABLE = ("kind", "title", "tags", "starts_at")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _check_references(session: Session, trip: Trip, values: dict[str, Any]) -> None:
    for field in ("location_id", "end_location_id"):
        location_id = values.get(field)
        if location_id is not None and session.get(Location, location_id) is None:
            raise _unprocessable(f"{field} does not match a location")

    traveler_id = values.get("traveler_id")
    if traveler_id is not None and traveler_id != trip.owner_id:
        if session.get(TripMember, (trip.id, traveler_id)) is None:
            raise _unprocessable("traveler_id must be the trip owner or a member")


def _get_item(session: Session, trip: Trip, item_id: uuid.UUID) -> TripItem:
    item = session.get(TripItem, item_id)
    if item is None or item.trip_id != trip.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip item not found")
    return item


@router.get("", response_model=list[TripItemRead])
def list_items(
    trip: ViewableTrip, session: SessionDep, kind: TripItemKind | None = None
) -> list[TripItem]:
    query = (
        select(TripItem)
        .where(TripItem.trip_id == trip.id)
        .options(
            selectinload(TripItem.location),
            selectinload(TripItem.end_location),
            selectinload(TripItem.traveler),
        )
        .order_by(TripItem.starts_at, TripItem.id)
    )
    if kind is not None:
        query = query.where(TripItem.kind == kind)
    return list(session.scalars(query))


@router.post("", response_model=TripItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: TripItemCreate, trip: EditableTrip, user: CurrentUser, session: SessionDep
) -> TripItem:
    values = payload.model_dump()
    _check_references(session, trip, values)
    item = TripItem(**values, trip_id=trip.id, created_by_id=user.id)
    session.add(item)
    session.commit()
    return item


@router.get("/{item_id}", response_model=TripItemRead)
def read_item(item_id: uuid.UUID, trip: ViewableTrip, session: SessionDep) -> TripItem:
    return _get_item(session, trip, item_id)


@router.patch("/{item_id}", response_model=TripItemRead)
def update_item(
    item_id: uuid.UUID, payload: TripItemUpdate, trip: EditableTrip, session: SessionDep
) -> TripItem:
    item = _get_item(session, trip, item_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise _unprocessable(f"{field} cannot be null")
    _check_references(session, trip, changes)

    starts_at = changes.get("starts_at", item.starts_at)
    ends_at = changes.get("ends_at", item.ends_at)
    if ends_at is not None and ends_at < starts_at:
        raise _unprocessable("ends_at must be at or after starts_at")

    for field, value in changes.items():
        setattr(item, field, value)
    session.commit()
    # Reload so the location/traveler relationships follow any changed ids.
    session.refresh(item)
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: uuid.UUID, trip: EditableTrip, session: SessionDep) -> None:
    session.delete(_get_item(session, trip, item_id))
    session.commit()
