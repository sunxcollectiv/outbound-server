import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, SessionDep, ViewableTrip, is_organizer
from app.models import (
    Location,
    RsvpStatus,
    Trip,
    TripItem,
    TripItemKind,
    TripItemParticipant,
    TripItemScope,
    TripItemVisibility,
    User,
)
from app.schemas import RsvpUpdate, TripItemCreate, TripItemRead, TripItemUpdate, check_scope_fields

router = APIRouter(prefix="/trips/{trip_id}/items", tags=["trip items"])

NOT_NULLABLE = ("kind", "title", "tags", "starts_at")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def visible_items(trip: Trip, user: User) -> Select[tuple[TripItem]]:
    """Items on the trip that `user` may see: every group item, their own
    items, anything shared or open, and anything they're a participant on."""
    joined = select(TripItemParticipant.item_id).where(TripItemParticipant.user_id == user.id)
    return (
        select(TripItem)
        .where(TripItem.trip_id == trip.id)
        .where(
            or_(
                TripItem.scope == TripItemScope.GROUP,
                TripItem.created_by_id == user.id,
                TripItem.visibility.in_([TripItemVisibility.SHARED, TripItemVisibility.OPEN]),
                TripItem.id.in_(joined),
            )
        )
    )


def with_item_details(query: Select[tuple[TripItem]]) -> Select[tuple[TripItem]]:
    """Eager-load everything TripItemRead embeds."""
    return query.options(
        selectinload(TripItem.location),
        selectinload(TripItem.end_location),
        selectinload(TripItem.created_by),
        selectinload(TripItem.participants).selectinload(TripItemParticipant.user),
    )


def _check_references(session: Session, values: dict[str, Any]) -> None:
    for field in ("location_id", "end_location_id"):
        location_id = values.get(field)
        if location_id is not None and session.get(Location, location_id) is None:
            raise _unprocessable(f"{field} does not match a location")


def _get_item(session: Session, trip: Trip, user: User, item_id: uuid.UUID) -> TripItem:
    """404s for items the caller can't see, so private items don't leak."""
    query = with_item_details(visible_items(trip, user)).where(TripItem.id == item_id)
    item = session.scalar(query)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip item not found")
    return item


def _require_can_modify(session: Session, trip: Trip, user: User, item: TripItem) -> None:
    """Group items belong to the organizers; personal items only to their creator."""
    if item.scope is TripItemScope.GROUP:
        if not is_organizer(session, trip, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can change group items")
    elif item.created_by_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the person who added this item can change it"
        )


def _refresh(session: Session, item: TripItem) -> TripItem:
    """Reload so embedded relationships follow any changed ids or new rows."""
    session.refresh(item)
    return item


@router.get("", response_model=list[TripItemRead])
def list_items(
    trip: ViewableTrip,
    user: CurrentUser,
    session: SessionDep,
    kind: TripItemKind | None = None,
    scope: TripItemScope | None = None,
) -> list[TripItem]:
    query = with_item_details(visible_items(trip, user)).order_by(TripItem.starts_at, TripItem.id)
    if kind is not None:
        query = query.where(TripItem.kind == kind)
    if scope is not None:
        query = query.where(TripItem.scope == scope)
    return list(session.scalars(query))


@router.post("", response_model=TripItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: TripItemCreate, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> TripItem:
    if payload.scope is TripItemScope.GROUP and not is_organizer(session, trip, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can add group items")
    values = payload.model_dump()
    _check_references(session, values)
    item = TripItem(**values, trip_id=trip.id, created_by_id=user.id)
    if item.scope is TripItemScope.PERSONAL:
        item.participants.append(TripItemParticipant(user_id=user.id, status=RsvpStatus.GOING))
    session.add(item)
    session.commit()
    return _refresh(session, item)


@router.get("/{item_id}", response_model=TripItemRead)
def read_item(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> TripItem:
    return _get_item(session, trip, user, item_id)


@router.patch("/{item_id}", response_model=TripItemRead)
def update_item(
    item_id: uuid.UUID,
    payload: TripItemUpdate,
    trip: ViewableTrip,
    user: CurrentUser,
    session: SessionDep,
) -> TripItem:
    item = _get_item(session, trip, user, item_id)
    _require_can_modify(session, trip, user, item)

    changes = payload.model_dump(exclude_unset=True)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise _unprocessable(f"{field} cannot be null")
    _check_references(session, changes)

    requirement = changes.get("requirement", item.requirement)
    visibility = changes.get("visibility", item.visibility)
    try:
        check_scope_fields(item.scope, requirement, visibility)
    except ValueError as exc:
        raise _unprocessable(str(exc)) from None
    if item.scope is TripItemScope.GROUP and requirement is None:
        raise _unprocessable("requirement cannot be null on a group item")
    if item.scope is TripItemScope.PERSONAL and visibility is None:
        raise _unprocessable("visibility cannot be null on a personal item")

    starts_at = changes.get("starts_at", item.starts_at)
    ends_at = changes.get("ends_at", item.ends_at)
    if ends_at is not None and ends_at < starts_at:
        raise _unprocessable("ends_at must be at or after starts_at")

    for field, value in changes.items():
        setattr(item, field, value)
    session.commit()
    return _refresh(session, item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    item = _get_item(session, trip, user, item_id)
    _require_can_modify(session, trip, user, item)
    session.delete(item)
    session.commit()


# --- rsvp ---


@router.put("/{item_id}/rsvp", response_model=TripItemRead)
def set_rsvp(
    item_id: uuid.UUID,
    payload: RsvpUpdate,
    trip: ViewableTrip,
    user: CurrentUser,
    session: SessionDep,
) -> TripItem:
    """Answer a group item, or join an open personal item. Upserts the caller's row."""
    item = _get_item(session, trip, user, item_id)
    if (
        item.scope is TripItemScope.PERSONAL
        and item.visibility is not TripItemVisibility.OPEN
        and item.created_by_id != user.id
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This item isn't open to join")

    row = next((p for p in item.participants if p.user_id == user.id), None)
    if row is None:
        item.participants.append(TripItemParticipant(user_id=user.id, status=payload.status))
    else:
        row.status = payload.status
    session.commit()
    return _refresh(session, item)


@router.delete("/{item_id}/rsvp", status_code=status.HTTP_204_NO_CONTENT)
def clear_rsvp(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    """Withdraw an answer on a group item, or leave a personal item you joined."""
    item = _get_item(session, trip, user, item_id)
    if item.scope is TripItemScope.PERSONAL and item.created_by_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't leave your own item; delete it")
    row = next((p for p in item.participants if p.user_id == user.id), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No RSVP to clear")
    item.participants.remove(row)  # delete-orphan cascade removes the row
    session.commit()
