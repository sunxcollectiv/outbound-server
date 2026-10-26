import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, SessionDep, ViewableTrip, is_organizer, member_for
from app.models import (
    Place,
    RsvpStatus,
    TransitMode,
    Trip,
    TripItem,
    TripItemKind,
    TripItemParticipant,
    TripItemScope,
    TripItemTransitSegment,
    TripItemVisibility,
    TripMember,
    User,
)
from app.schemas import (
    ParticipantSet,
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
    joined = (
        select(TripItemParticipant.item_id)
        .join(TripMember, TripMember.id == TripItemParticipant.member_id)
        .where(TripMember.user_id == user.id)
    )
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
        selectinload(TripItem.participants)
        .selectinload(TripItemParticipant.member)
        .selectinload(TripMember.user),
        selectinload(TripItem.transit_segments).selectinload(
            TripItemTransitSegment.place
        ).selectinload(Place.area),
        selectinload(TripItem.transit_segments).selectinload(
            TripItemTransitSegment.end_place
        ).selectinload(Place.area),
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


def _in_chronological_order(
    earlier: datetime, earlier_tz: str | None, later: datetime, later_tz: str | None
) -> bool:
    """Whether `later` falls at or after `earlier` — by instant when both ends
    are zoned, by wall clock otherwise (a westbound hop can land at an earlier
    local time than it left)."""
    if earlier_tz is not None and later_tz is not None:
        return to_utc(later, later_tz) >= to_utc(earlier, earlier_tz)
    return later >= earlier


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

    if ends_at is not None and not _in_chronological_order(starts_at, start_tz, ends_at, end_tz):
        raise _unprocessable("ends_at must be at or after starts_at")


SPAN_FIELDS = ("starts_at", "ends_at", "start_timezone", "end_timezone", "place_id", "end_place_id")


def _leg_from_span(values: dict[str, Any]) -> dict[str, Any]:
    """The lone leg of a plain point-to-point transit: its ends are the item's,
    and it inherits the item's mode. The booking reference stays on the item."""
    leg = {field: values.get(field) for field in SPAN_FIELDS}
    leg["carrier"] = leg["flight_number"] = None
    leg["transit_mode"] = values.get("transit_mode")
    leg["confirmation_number"] = None
    return leg


def _build_segments(
    session: Session, payloads: list[dict[str, Any]]
) -> list[TripItemTransitSegment]:
    """Settle each leg's places and zones (like a standalone item end), then
    check the legs run forward: a leg may not depart before the one before it
    arrives. Returns the legs in order, ready to attach to the booking."""
    segments: list[TripItemTransitSegment] = []
    for position, payload in enumerate(payloads):
        leg = dict(payload)
        check_places(session, leg)
        _resolve_times(session, leg)
        segments.append(TripItemTransitSegment(position=position, **leg))

    for arriving, departing in zip(segments, segments[1:]):
        if arriving.ends_at is not None and not _in_chronological_order(
            arriving.ends_at, arriving.end_timezone, departing.starts_at, departing.start_timezone
        ):
            raise _unprocessable(
                "each transit leg must depart at or after the previous leg arrives"
            )
    return segments


def _apply_span(values: dict[str, Any], segments: list[TripItemTransitSegment]) -> None:
    """Carry the booking's own span down from its legs: it departs when the
    first leg departs and arrives when the last leg arrives. The overall mode is
    the shared leg mode when they all agree, `mixed` when they differ (null when
    no leg names one)."""
    first, last = segments[0], segments[-1]
    values["starts_at"], values["start_timezone"] = first.starts_at, first.start_timezone
    values["place_id"] = first.place_id
    values["ends_at"], values["end_timezone"] = last.ends_at, last.end_timezone
    values["end_place_id"] = last.end_place_id
    modes = {s.transit_mode for s in segments if s.transit_mode is not None}
    values["transit_mode"] = modes.pop() if len(modes) == 1 else (TransitMode.MIXED if modes else None)


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


def _creator_member(session: Session, trip: Trip, item: TripItem) -> TripMember:
    """The member row of a personal item's creator. Personal items always have
    a creator on the trip: leaving takes them with you."""
    member = member_for(session, trip, item.created_by_id)
    assert member is not None, "personal item whose creator isn't on the trip"
    return member


def _settle_participants(session: Session, trip: Trip, item: TripItem) -> None:
    """Make the participant rows fit the item's scope.

    Personal items list who's in, so a 'declined' RSVP from the group days is
    dropped and the creator is always in. Group items keep rows as RSVPs."""
    if item.scope is TripItemScope.PERSONAL:
        for row in list(item.participants):
            if row.status is RsvpStatus.DECLINED:
                item.participants.remove(row)
        creator = _creator_member(session, trip, item)
        if not any(row.member_id == creator.id for row in item.participants):
            item.participants.append(
                TripItemParticipant(member_id=creator.id, status=RsvpStatus.GOING)
            )


def _set_participants(
    session: Session, trip: Trip, item: TripItem, entries: list[ParticipantSet]
) -> None:
    """Replace the item's answers with `entries`: listed members get the status
    given (keeping their row if they had one), everyone else's is dropped.
    Every member must be on this trip. Call after the item's scope is final,
    since a personal item only lists who's in and always includes its creator."""
    known = set(session.scalars(select(TripMember.id).where(TripMember.trip_id == trip.id)))
    unknown = [str(entry.member_id) for entry in entries if entry.member_id not in known]
    if unknown:
        raise _unprocessable(f"Not on this trip: {', '.join(unknown)}")
    if item.scope is TripItemScope.PERSONAL and any(
        entry.status is RsvpStatus.DECLINED for entry in entries
    ):
        raise _unprocessable(
            "a personal item lists who's in; leave people out instead of declining"
        )

    wanted = {entry.member_id: entry.status for entry in entries}
    for row in list(item.participants):
        if row.member_id in wanted:
            row.status = wanted.pop(row.member_id)
        else:
            item.participants.remove(row)
    for member_id, rsvp in wanted.items():
        item.participants.append(TripItemParticipant(member_id=member_id, status=rsvp))
    _settle_participants(session, trip, item)


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


def build_trip_item(
    session: Session, trip: Trip, user: User, payload: TripItemCreate
) -> TripItem:
    """Build a TripItem (plus its transit legs) from a create payload and add it
    to the session — shared by the create route and the mail-import stage, so
    segment building, span roll-up, and time/zone resolution stay in one place.
    The caller commits. Does not do the group-organizer permission check (that's
    the route's job); import only ever creates personal items."""
    values = payload.model_dump()
    segments_payload = values.pop("segments", None)
    del values["participants"]
    if payload.kind is TripItemKind.TRANSIT:
        # A transit always has at least one leg: its own ends when none is given.
        segments = _build_segments(session, segments_payload or [_leg_from_span(values)])
        _apply_span(values, segments)
    else:
        check_places(session, values)
        _resolve_times(session, values)
        segments = []
    item = TripItem(**values, trip_id=trip.id, created_by_id=user.id)
    item.transit_segments = segments
    if payload.participants is not None:
        _set_participants(session, trip, item, payload.participants)
    else:
        _settle_participants(session, trip, item)
    session.add(item)
    return item


@router.post("", response_model=TripItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: TripItemCreate, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> TripItem:
    if payload.scope is TripItemScope.GROUP and not is_organizer(session, trip, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only organizers can add group items")
    item = build_trip_item(session, trip, user, payload)
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
    segments_payload = changes.pop("segments", None)
    changes.pop("participants", None)
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

    resulting_kind = changes.get("kind", item.kind)
    now_transit = resulting_kind is TripItemKind.TRANSIT
    span_edited = any(field in changes for field in SPAN_FIELDS)

    if segments_payload is not None:
        if not now_transit:
            raise _unprocessable("only transit items have segments")
        if not segments_payload:
            raise _unprocessable("a transit item needs at least one segment")
        if span_edited:
            raise _unprocessable(
                "set a segmented transit's times and places on its segments, not the item"
            )
        segments = _build_segments(session, segments_payload)
        item.transit_segments = segments
        _apply_span(changes, segments)
    elif now_transit:
        _resolve_times(session, changes, item)
        # Keep the lone leg in step with the item's own ends when they're edited,
        # or mint one when an item is turning into transit. A booking with
        # layovers can only be changed through its segments.
        if span_edited or item.kind is not TripItemKind.TRANSIT:
            if len(item.transit_segments) > 1:
                raise _unprocessable(
                    "this transit has layovers; edit its times and places through its segments"
                )
            existing = item.transit_segments[0] if item.transit_segments else None
            span = {field: changes.get(field, getattr(item, field)) for field in SPAN_FIELDS}
            item.transit_segments = [
                TripItemTransitSegment(
                    position=0,
                    carrier=existing.carrier if existing else None,
                    flight_number=existing.flight_number if existing else None,
                    **span,
                )
            ]
    else:
        _resolve_times(session, changes, item)
        if item.kind is TripItemKind.TRANSIT:  # leaving transit drops its legs
            item.transit_segments = []

    for field, value in changes.items():
        setattr(item, field, value)
    if payload.participants is not None:
        _set_participants(session, trip, item, payload.participants)
    elif "scope" in changes:
        _settle_participants(session, trip, item)
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

    member = member_for(session, trip, user.id)
    row = next((p for p in item.participants if p.member_id == member.id), None)
    if row is None:
        item.participants.append(TripItemParticipant(member_id=member.id, status=payload.status))
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
    member = member_for(session, trip, user.id)
    row = next((p for p in item.participants if p.member_id == member.id), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No RSVP to clear")
    item.participants.remove(row)  # delete-orphan cascade removes the row
    session.commit()
