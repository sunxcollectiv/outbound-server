import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, SessionDep, ViewableTrip, is_organizer
from app.models import (
    Place,
    RsvpStatus,
    Trip,
    TripItem,
    TripItemKind,
    TripItemParticipant,
    TripItemScope,
    TripItemVisibility,
    User,
)
from app.schemas import (
    RsvpUpdate,
    TripItemCreate,
    TripItemRead,
    TripItemUpdate,
    resolve_scope_fields,
)
from app.services.timekeeping import to_local, to_utc

router = APIRouter(prefix="/trips/{trip_id}/items", tags=["trip items"])

NOT_NULLABLE = ("kind", "scope", "title", "tags", "starts_at")


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
        selectinload(TripItem.place).selectinload(Place.area),
        selectinload(TripItem.end_place).selectinload(Place.area),
        selectinload(TripItem.created_by),
        selectinload(TripItem.participants).selectinload(TripItemParticipant.user),
    )


def check_places(session: Session, values: dict[str, Any]) -> None:
    """422 unless every place id in `values` points at a real place."""
    for field in ("place_id", "end_place_id"):
        place_id = values.get(field)
        if place_id is not None and session.get(Place, place_id) is None:
            raise _unprocessable(f"{field} does not match a place")


def _place_timezone(session: Session, place_id: uuid.UUID | None) -> str | None:
    if place_id is None:
        return None
    place = session.get(Place, place_id)
    return place.timezone if place is not None else None


def _resolve_times(
    session: Session, values: dict[str, Any], item: TripItem | None = None
) -> None:
    """Settle the local time + zone pairs in `values`: a create payload, or the
    changes of a PATCH on `item`. Call check_places first.

    A zone is only inferred when nothing supplies one: the request, else the
    item's stored zone, else the place at that end; the arrival then falls
    back to the departure's zone. With no zone at all the time floats, which
    is the normal case for "dinner at 7 on the 17th". So an un-enriched end
    place leaves a transit's arrival in its origin zone, and importers should
    send end_timezone. Sending `start_timezone: null` on a PATCH makes an
    item float again.

    Times are then normalised to wall time in their zone (aware input is
    converted, so it needs a zone) and the end may not precede the start: as
    instants when zoned, as wall times when floating.
    """

    def effective(field: str) -> Any:
        if field in values:
            return values[field]
        return getattr(item, field) if item is not None else None

    starts_at, ends_at = effective("starts_at"), effective("ends_at")

    if item is not None and "start_timezone" in values:
        start_tz = values["start_timezone"]  # explicit; null means floating
    else:
        start_tz = effective("start_timezone") or _place_timezone(session, effective("place_id"))

    if ends_at is None or start_tz is None:
        if values.get("end_timezone") is not None:
            raise _unprocessable("end_timezone needs an ends_at and a start_timezone")
        end_tz = None
    else:
        end_tz = (
            effective("end_timezone")
            or _place_timezone(session, effective("end_place_id"))
            or start_tz
        )

    values["start_timezone"], values["end_timezone"] = start_tz, end_tz
    for field, tz in (("starts_at", start_tz), ("ends_at", end_tz)):
        if field in values and values[field] is not None:
            if tz is None and values[field].tzinfo is not None:
                raise _unprocessable(
                    f"{field} has an offset but no zone to read it in; send a timezone or a located place"
                )
            values[field] = to_local(values[field], tz) if tz else values[field]
    starts_at, ends_at = effective("starts_at"), effective("ends_at")

    if ends_at is not None:
        if start_tz is not None:
            in_order = to_utc(ends_at, end_tz) >= to_utc(starts_at, start_tz)
        else:
            in_order = ends_at >= starts_at
        if not in_order:
            raise _unprocessable("ends_at must be at or after starts_at")


def _get_item(session: Session, trip: Trip, user: User, item_id: uuid.UUID) -> TripItem:
    """404s for items the caller can't see, so private items don't leak."""
    query = with_item_details(visible_items(trip, user)).where(TripItem.id == item_id)
    item = session.scalar(query)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip item not found")
    return item


def _require_can_modify(
    session: Session,
    trip: Trip,
    user: User,
    item: TripItem,
    as_scope: TripItemScope | None = None,
) -> None:
    """Group items belong to the organizers; personal items only to their creator.
    Pass as_scope to check against a scope the item is about to take."""
    scope = as_scope or item.scope
    if scope is TripItemScope.GROUP:
        if not is_organizer(session, trip, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can change group items")
    elif item.created_by_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the person who added this item can change it"
        )


def _settle_participants(item: TripItem) -> None:
    """After a scope change, make the participant rows fit the new meaning.

    Personal items list who's in, so a 'declined' RSVP from the group days is
    dropped and the creator is always in. Group items keep rows as RSVPs."""
    if item.scope is TripItemScope.PERSONAL:
        for row in list(item.participants):
            if row.status is RsvpStatus.DECLINED:
                item.participants.remove(row)
        if not any(row.user_id == item.created_by_id for row in item.participants):
            item.participants.append(
                TripItemParticipant(user_id=item.created_by_id, status=RsvpStatus.GOING)
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
    # Wall-time order: floating items have no instant to sort by.
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
    check_places(session, values)
    _resolve_times(session, values)
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
    check_places(session, changes)

    scope = changes.get("scope", item.scope)
    if scope is not item.scope:
        # The caller must be allowed to modify the item in its new form too, and
        # the item keeps its creator, who is who a personal item belongs to.
        _require_can_modify(session, trip, user, item, as_scope=scope)
        if scope is TripItemScope.PERSONAL and item.created_by_id is None:
            raise _unprocessable("This item's creator has left the trip; it can't become personal")
        # The old scope's field doesn't carry over; defaults fill whatever wasn't sent.
        requirement, visibility = changes.get("requirement"), changes.get("visibility")
    else:
        # Clearing the field the current scope needs is an error, not a reset.
        own_field = "requirement" if scope is TripItemScope.GROUP else "visibility"
        if own_field in changes and changes[own_field] is None:
            raise _unprocessable(f"{own_field} cannot be null on a {scope.value} item")
        requirement = changes.get("requirement", item.requirement)
        visibility = changes.get("visibility", item.visibility)
    try:
        changes["requirement"], changes["visibility"] = resolve_scope_fields(
            scope, requirement, visibility
        )
    except ValueError as exc:
        raise _unprocessable(str(exc)) from None

    _resolve_times(session, changes, item)

    for field, value in changes.items():
        setattr(item, field, value)
    if "scope" in changes:
        _settle_participants(item)
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
