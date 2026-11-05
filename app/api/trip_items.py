import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, Now, SessionDep, ViewableTrip, member_for
from app.models import (
    EmailReceipt,
    ImageReceipt,
    Place,
    TransitMode,
    Trip,
    TripItem,
    TripItemKind,
    TripItemLodging,
    TripItemLodgingRoom,
    TripItemParticipant,
    TripItemTransitSegment,
    TripMember,
    User,
)
from app.schemas import (
    LodgingRoomSet,
    ParticipantSet,
    RsvpUpdate,
    TripItemCreate,
    TripItemRead,
    TripItemUpdate,
)
from app.services.item_edits import booking_state
from app.services.item_notifications import (
    ItemSnapshot,
    notify_item_created,
    notify_item_updated,
)
from app.services.item_visibility import present_item, visible_items
from app.services.timekeeping import to_local, to_utc

router = APIRouter(prefix="/trips/{trip_id}/items", tags=["trip items"])

NOT_NULLABLE = ("kind", "visibility", "tags", "starts_at")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _check_named(title: str | None, place_id: uuid.UUID | None) -> None:
    """422 unless the item has a title or a place to go by. Checked on the
    settled values, since a segmented transit's place comes from its first leg."""
    if title is None and place_id is None:
        raise _unprocessable("an item needs a title or a place")


def _check_stay(kind: TripItemKind, starts_at: datetime, ends_at: datetime | None) -> None:
    """422 unless a stay has a check-out on a later date than its check-in
    (local dates, as each end is written): at least one night."""
    if kind is not TripItemKind.LODGING:
        return
    if ends_at is None:
        raise _unprocessable("a stay needs a check-out (ends_at)")
    if ends_at.date() <= starts_at.date():
        raise _unprocessable("a stay must check out on a later date than it checks in")


def viewer(session: Session, trip: Trip, user: User) -> TripMember:
    """The caller's member row. ViewableTrip has already checked there is one."""
    member = member_for(session, trip, user.id)
    assert member is not None, "viewable trip without a member row"
    return member


def with_item_details(query: Select[tuple[TripItem]]) -> Select[tuple[TripItem]]:
    """Eager-load everything TripItemRead embeds."""
    return query.options(
        selectinload(TripItem.place).selectinload(Place.area),
        selectinload(TripItem.end_place).selectinload(Place.area),
        selectinload(TripItem.created_by),
        selectinload(TripItem.edited_by),
        selectinload(TripItem.lodging),
        selectinload(TripItem.rooms),
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


def _set_lodging(item: TripItem, details: dict[str, Any] | None, start_tz: str | None) -> None:
    """Make `details` the stay's details (None drops them). The cancellation
    deadline is a wall time at the property, read in the stay's check-in zone;
    an aware one is converted into that zone, so it needs one. An existing row
    is updated in place rather than swapped for a new one with the same key."""
    if details is None:
        item.lodging = None
        return
    until = details.get("free_cancellation_until")
    if until is not None and until.tzinfo is not None:
        if start_tz is None:
            raise _unprocessable(
                "free_cancellation_until has an offset but the stay has no zone to read it in"
            )
        details["free_cancellation_until"] = to_local(until, start_tz)
    row = item.lodging or TripItemLodging()
    for field in TripItemLodging.FIELDS:
        setattr(row, field, details.get(field))
    item.lodging = row


def _trip_member_ids(session: Session, trip: Trip) -> set[uuid.UUID]:
    """Every member id on the trip: who an item's participants and room
    occupants may be. Looked up once per request and shared."""
    return set(session.scalars(select(TripMember.id).where(TripMember.trip_id == trip.id)))


def _set_rooms(
    item: TripItem, rooms: list[LodgingRoomSet], known: set[uuid.UUID]
) -> None:
    """Make `rooms` the stay's rooms, in order, and put each listed occupant in
    their room: rooms sent with an id are kept (and updated), new ones made,
    unlisted ones dropped. Occupants not yet participants become participants;
    every other participant ends up in no room. Call after any participants
    edit, so it has the last word on who's where."""
    existing = {room.id: room for room in item.rooms}
    unknown = [str(room.id) for room in rooms if room.id is not None and room.id not in existing]
    if unknown:
        raise _unprocessable(f"Not a room of this stay: {', '.join(unknown)}")
    strangers = {str(m) for room in rooms for m in room.occupants if m not in known}
    if strangers:
        raise _unprocessable(f"Not on this trip: {', '.join(sorted(strangers))}")

    kept: list[TripItemLodgingRoom] = []
    room_of: dict[uuid.UUID, TripItemLodgingRoom] = {}
    for position, entry in enumerate(rooms):
        # A new room gets its id now, so assignments can point at it pre-flush.
        row = existing[entry.id] if entry.id is not None else TripItemLodgingRoom(id=uuid.uuid4())
        row.position = position
        for field in TripItemLodgingRoom.FIELDS:
            setattr(row, field, getattr(entry, field))
        kept.append(row)
        room_of.update({member_id: row for member_id in entry.occupants})
    item.rooms = kept

    on_item = {row.member_id for row in item.participants}
    for member_id in room_of.keys() - on_item:
        item.participants.append(TripItemParticipant(member_id=member_id))
    for row in item.participants:
        _assign(row, room_of.get(row.member_id))


def _clear_rooms(item: TripItem) -> None:
    """Drop a stay's rooms and everyone's place in them."""
    if item.rooms:
        item.rooms = []
    for row in item.participants:
        _assign(row, None)


def _assign(row: TripItemParticipant, room: TripItemLodgingRoom | None) -> None:
    # Only touch rows whose room actually changes, so an edit doesn't bump
    # every participant's updated_at.
    if row.room_id != (room.id if room is not None else None):
        row.room = room


def _get_item(
    session: Session, trip: Trip, member: TripMember, item_id: uuid.UUID
) -> TripItem:
    """404s for items the caller can't see, so private items don't leak."""
    query = with_item_details(visible_items(trip, member)).where(TripItem.id == item_id)
    item = session.scalar(query)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip item not found")
    return item


def _set_participants(
    item: TripItem, entries: list[ParticipantSet], known: set[uuid.UUID]
) -> None:
    """Make `entries` the item's participants. Listed members keep their row
    and answer (unless the entry sends a new status), new ones are added,
    anyone not listed is dropped. Every member must be on this trip (`known`,
    from _trip_member_ids)."""
    unknown = [str(entry.member_id) for entry in entries if entry.member_id not in known]
    if unknown:
        raise _unprocessable(f"Not on this trip: {', '.join(unknown)}")

    wanted = {entry.member_id: entry for entry in entries}
    for row in list(item.participants):
        entry = wanted.pop(row.member_id, None)
        if entry is None:
            item.participants.remove(row)
        elif "status" in entry.model_fields_set:
            row.status = entry.status
    for member_id, entry in wanted.items():
        item.participants.append(TripItemParticipant(member_id=member_id, status=entry.status))


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
) -> list[TripItemRead]:
    """Every item on the trip the caller may see, in wall-clock order. Optional
    `kind` filters to one type (lodging/transit/activity/…). Private items show
    only to their participants; closed items are redacted for non-participants."""
    member = viewer(session, trip, user)
    # Wall-time order: floating items have no instant to sort by.
    query = with_item_details(visible_items(trip, member)).order_by(
        TripItem.starts_at, TripItem.id
    )
    if kind is not None:
        query = query.where(TripItem.kind == kind)
    return [present_item(item, member.id) for item in session.scalars(query)]


def build_trip_item(
    session: Session, trip: Trip, user: User, payload: TripItemCreate
) -> TripItem:
    """Build a TripItem (plus its transit legs) from a create payload and add it
    to the session — shared by the create route and the mail-import stage, so
    segment building, span roll-up, and time/zone resolution stay in one place.
    The caller commits. With no participants given, the item is just for its
    creator."""
    values = payload.model_dump()
    segments_payload = values.pop("segments", None)
    lodging_payload = values.pop("lodging", None)
    values.pop("rooms", None)
    del values["participants"]
    del values["notify_members"]
    if payload.kind is TripItemKind.TRANSIT:
        # A transit always has at least one leg: its own ends when none is given.
        segments = _build_segments(session, segments_payload or [_leg_from_span(values)])
        _apply_span(values, segments)
    else:
        check_places(session, values)
        _resolve_times(session, values)
        segments = []
    _check_named(values["title"], values["place_id"])
    _check_stay(payload.kind, values["starts_at"], values["ends_at"])
    item = TripItem(**values, trip_id=trip.id, created_by_id=user.id)
    item.transit_segments = segments
    _set_lodging(item, lodging_payload, values["start_timezone"])
    known = (
        _trip_member_ids(session, trip)
        if payload.participants is not None or payload.rooms
        else set()
    )
    if payload.participants is not None:
        _set_participants(item, payload.participants, known)
    else:
        item.participants.append(TripItemParticipant(member_id=viewer(session, trip, user).id))
    if payload.rooms:
        _set_rooms(item, payload.rooms, known)
    session.add(item)
    return item


@router.post("", response_model=TripItemRead, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: TripItemCreate, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> TripItemRead:
    """Anyone on the trip can add an item, for any of its members. With
    `notify_members`, the others who can see it are told."""
    item = build_trip_item(session, trip, user, payload)
    if payload.notify_members:
        notify_item_created(session, trip, item, user)
    session.commit()
    return present_item(_refresh(session, item), viewer(session, trip, user).id)


@router.get("/{item_id}", response_model=TripItemRead)
def read_item(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> TripItemRead:
    """One item by id, as the caller is allowed to see it (404 if they can't)."""
    member = viewer(session, trip, user)
    return present_item(_get_item(session, trip, member, item_id), member.id)


# The original is someone's own booking, served from our origin: never cache it
# in shared caches, never sniff it into something else, and render an email's
# HTML with scripts, forms and remote loads (other than images) shut off.
_ORIGINAL_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
    "Content-Security-Policy": (
        "sandbox; default-src 'none'; img-src * data:; style-src 'unsafe-inline'"
    ),
}


@router.get(
    "/{item_id}/source",
    response_class=Response,
    responses={
        200: {
            "description": "The original: the image as uploaded, or the email's HTML "
            "(plain text when it had none).",
            "content": {"image/*": {}, "text/html": {}, "text/plain": {}},
        }
    },
)
def read_source(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> Response:
    """The original an imported item was read from — the uploaded screenshot,
    or the forwarded email — so its participants can check the item against
    it or show it to staff. Participants only (it can carry other travelers'
    details): 403 for anyone else who can see the item. 404 when the item
    wasn't imported, or its original is gone."""
    member = viewer(session, trip, user)
    item = _get_item(session, trip, member, item_id)
    no_original = HTTPException(status.HTTP_404_NOT_FOUND, "This item has no original to show")
    if not item.has_source:
        raise no_original
    # Checked before loading the original, which can be megabytes.
    if not any(row.member_id == member.id for row in item.participants):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the item's participants can view its original"
        )

    if item.source_image_id is not None:
        image = session.get(ImageReceipt, item.source_image_id)
        assert image is not None, "the link is cleared when the image is deleted"
        media_type = (
            image.content_type
            if image.content_type.startswith("image/")
            else "application/octet-stream"
        )
        return Response(image.data, media_type=media_type, headers=_ORIGINAL_HEADERS)
    email = session.get(EmailReceipt, item.source_email_receipt_id)
    raw = (email.raw if email is not None else None) or {}
    if raw.get("html"):
        return Response(
            raw["html"], media_type="text/html; charset=utf-8", headers=_ORIGINAL_HEADERS
        )
    if raw.get("text"):
        return Response(
            raw["text"], media_type="text/plain; charset=utf-8", headers=_ORIGINAL_HEADERS
        )
    raise no_original


@router.patch("/{item_id}", response_model=TripItemRead)
def update_item(
    item_id: uuid.UUID,
    payload: TripItemUpdate,
    trip: ViewableTrip,
    user: CurrentUser,
    session: SessionDep,
    now: Now,
) -> TripItemRead:
    """Anyone who can see the item can change it, for now. With
    `notify_members`, the others who can see it are told what changed. A
    change to the booking itself stamps `edited_at`/`edited_by` (see
    app.services.item_edits)."""
    member = viewer(session, trip, user)
    item = _get_item(session, trip, member, item_id)
    before = ItemSnapshot.of(item) if payload.notify_members else None
    booking_before = booking_state(item)

    changes = payload.model_dump(exclude_unset=True)
    segments_payload = changes.pop("segments", None)
    lodging_sent = "lodging" in changes
    lodging_payload = changes.pop("lodging", None)
    rooms_sent = "rooms" in changes
    changes.pop("rooms", None)
    changes.pop("participants", None)
    changes.pop("notify_members", None)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise _unprocessable(f"{field} cannot be null")
    check_places(session, changes)

    resulting_kind = changes.get("kind", item.kind)
    now_transit = resulting_kind is TripItemKind.TRANSIT
    now_lodging = resulting_kind is TripItemKind.LODGING
    if lodging_payload is not None and not now_lodging:
        raise _unprocessable("only lodging items have lodging details")
    if payload.rooms and not now_lodging:
        raise _unprocessable("only lodging items have rooms")
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

    _check_named(changes.get("title", item.title), changes.get("place_id", item.place_id))
    _check_stay(
        resulting_kind,
        changes.get("starts_at", item.starts_at),
        changes.get("ends_at", item.ends_at),
    )
    if lodging_sent:
        _set_lodging(item, lodging_payload, changes.get("start_timezone", item.start_timezone))
    elif not now_lodging:  # leaving lodging drops the stay's details
        item.lodging = None
    for field, value in changes.items():
        setattr(item, field, value)
    known = (
        _trip_member_ids(session, trip)
        if payload.participants is not None or payload.rooms
        else set()
    )
    if payload.participants is not None:
        _set_participants(item, payload.participants, known)
    # Leaving lodging drops the rooms too; `rooms: []`/null clears them.
    if not now_lodging or (rooms_sent and not payload.rooms):
        _clear_rooms(item)
    elif payload.rooms:
        _set_rooms(item, payload.rooms, known)
    if booking_state(item) != booking_before:
        item.edited_at, item.edited_by = now, user
    if before is not None:
        notify_item_updated(session, trip, item, user, before)
    session.commit()
    return present_item(_refresh(session, item), member.id)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    """Delete an item the caller can see. (Anyone who can see it may delete it, for now.)"""
    item = _get_item(session, trip, viewer(session, trip, user), item_id)
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
) -> TripItemRead:
    """Record the caller's answer, adding them as a participant if they weren't."""
    member = viewer(session, trip, user)
    item = _get_item(session, trip, member, item_id)
    row = next((p for p in item.participants if p.member_id == member.id), None)
    if row is None:
        item.participants.append(TripItemParticipant(member_id=member.id, status=payload.status))
    else:
        row.status = payload.status
    session.commit()
    return present_item(_refresh(session, item), member.id)


@router.delete("/{item_id}/rsvp", status_code=status.HTTP_204_NO_CONTENT)
def clear_rsvp(
    item_id: uuid.UUID, trip: ViewableTrip, user: CurrentUser, session: SessionDep
) -> None:
    """Withdraw the caller's answer. They stay a participant; leaving an item is
    an edit to its participants."""
    member = viewer(session, trip, user)
    item = _get_item(session, trip, member, item_id)
    row = next((p for p in item.participants if p.member_id == member.id), None)
    if row is None or row.status is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No RSVP to clear")
    row.status = None
    session.commit()
