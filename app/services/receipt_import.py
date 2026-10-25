"""Import stage: turn an ``extracted`` receipt (email or image) into trip item(s)
in an existing trip the owner is a member of, updating the receipt to
``processed``.

Source-neutral: an ``EmailReceipt`` and an ``ImageReceipt`` carry the same fields
this stage touches (``user_id`` — the matched sender or the uploader —,
``status``, ``extracted``, ``trip_id``), and the extracted booking has the same
shape whatever produced it, so one importer serves both. The per-source routes
pass their own receipt in.

The trip is auto-matched by date overlap (dates-only for MVP): a unique
overlapping member-trip is used; zero or several leaves the receipt ``extracted``
with a ``no_match``/``ambiguous`` outcome (we never guess a placement). Creating a
new trip is out of scope.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.trip_items import build_trip_item
from app.models import (
    EmailReceipt,
    EmailReceiptStatus,
    ImageReceipt,
    Trip,
    TripMember,
    User,
)
from app.schemas import TripItemCreate
from app.services.place_resolver import resolve_place

Receipt = EmailReceipt | ImageReceipt
PlaceCache = dict  # (norm_name, norm_address) -> place id, per import call

Outcome = Literal["placed", "no_match", "ambiguous", "skipped"]

# The statuses the extract step will (re)run on; terminal ones are left alone, so
# process is safe to re-run. Mirrors the per-source EXTRACTABLE_STATUSES.
EXTRACTABLE_STATUSES = (EmailReceiptStatus.RECEIVED, EmailReceiptStatus.PENDING)


@dataclass
class ImportOutcome:
    receipt: Receipt
    outcome: Outcome
    trip_id: Any = None  # uuid.UUID | None
    item_count: int = 0
    candidate_trip_ids: list[Any] = field(default_factory=list)


def _as_datetime(local: Any) -> datetime | None:
    if not isinstance(local, str):
        return None
    try:
        return datetime.fromisoformat(local)
    except ValueError:
        return None


def _as_date(local: Any) -> date | None:
    dt = _as_datetime(local)
    return dt.date() if dt else None


def _booking_date_span(extraction: dict[str, Any]) -> tuple[date, date] | None:
    """The [earliest, latest] local date across the booking's items and legs."""
    dates: list[date] = []
    for item in extraction.get("items") or []:
        parts: list[dict[str, Any]] = [item, *(item.get("segments") or [])]
        for part in parts:
            for key in ("start_local", "end_local"):
                d = _as_date(part.get(key))
                if d is not None:
                    dates.append(d)
    if not dates:
        return None
    return min(dates), max(dates)


def _match_trip(
    session: Session, user_id: Any, span: tuple[date, date] | None
) -> tuple[Trip | None, list[Trip]]:
    """A unique member-trip whose dates overlap the booking, else (None,
    candidates). Overlap = trip.start <= booking_end and trip.end >= booking_start."""
    if span is None:
        return None, []
    start, end = span
    member_trip_ids = select(TripMember.trip_id).where(TripMember.user_id == user_id)
    candidates = list(
        session.scalars(
            select(Trip)
            .where(Trip.id.in_(member_trip_ids), Trip.start_date <= end, Trip.end_date >= start)
            .order_by(Trip.start_date, Trip.id)
        )
    )
    return (candidates[0] if len(candidates) == 1 else None), candidates


def _ends(part: dict[str, Any]) -> dict[str, Any]:
    return {
        "starts_at": _as_datetime(part.get("start_local")),
        "start_timezone": part.get("start_timezone"),
        "ends_at": _as_datetime(part.get("end_local")),
        "end_timezone": part.get("end_timezone"),
    }


def _item_payload(
    session: Session, item: dict[str, Any], *, user_id: Any, cache: PlaceCache
) -> TripItemCreate:
    """Map one extracted item to a create payload, resolving its places (airports/
    hotels) to Place ids via the shared resolver as we go."""
    kind = item.get("kind") or "activity"

    def place_id(descriptor: dict[str, Any] | None, tz: str | None) -> Any:
        return resolve_place(session, descriptor, timezone=tz, created_by_id=user_id, cache=cache)

    values: dict[str, Any] = {
        "kind": kind,
        "scope": "personal",
        "title": (item.get("title") or "Booking")[:200],
        "confirmation_number": item.get("confirmation_number"),
    }
    segments = item.get("segments") or []
    if kind == "transit" and segments:
        values["segments"] = [
            {
                **_ends(seg),
                "carrier": seg.get("carrier"),
                "flight_number": seg.get("flight_number"),
                "transit_mode": seg.get("transit_mode"),
                "confirmation_number": seg.get("confirmation_number"),
                "place_id": place_id(seg.get("origin"), seg.get("start_timezone")),
                "end_place_id": place_id(seg.get("destination"), seg.get("end_timezone")),
            }
            for seg in segments
        ]
    else:
        values.update(_ends(item))
        values["place_id"] = place_id(item.get("place"), item.get("start_timezone"))
        values["end_place_id"] = place_id(item.get("end_place"), item.get("end_timezone"))
        if kind == "transit":
            values["transit_mode"] = item.get("transit_mode")
    return TripItemCreate(**values)


def import_receipt(session: Session, receipt: Receipt) -> ImportOutcome:
    """Place an extracted receipt's booking into the owner's matching trip. Only
    acts on `extracted` receipts; the caller commits (atomic per receipt)."""
    if receipt.status is not EmailReceiptStatus.EXTRACTED:
        return ImportOutcome(receipt, "skipped")

    extraction = receipt.extracted or {}
    trip, candidates = _match_trip(session, receipt.user_id, _booking_date_span(extraction))
    if trip is None:
        outcome: Outcome = "ambiguous" if candidates else "no_match"
        return ImportOutcome(receipt, outcome, candidate_trip_ids=[t.id for t in candidates])

    # TODO(monetization): refuse placing into a locked/unpaid trip here — a trip
    # the user hasn't paid for should block this step. See [[monetization-model]].
    user = session.get(User, receipt.user_id)
    cache: PlaceCache = {}  # one place per booking (a layover airport resolves once)
    count = 0
    for item in extraction.get("items") or []:
        payload = _item_payload(session, item, user_id=receipt.user_id, cache=cache)
        build_trip_item(session, trip, user, payload)
        count += 1

    receipt.status = EmailReceiptStatus.PROCESSED
    receipt.trip_id = trip.id
    return ImportOutcome(receipt, "placed", trip_id=trip.id, item_count=count)


def process_receipt(
    session: Session, receipt: Receipt, *, extract: Callable[[], None]
) -> ImportOutcome:
    """Run the full pipeline on one receipt in a single pass: extract (if it's
    still awaiting it), then import. `extract` is the source-specific extract
    step as a no-arg callable (mail needs Resend + its extractor, image just its
    extractor), run in place on the receipt. Import only fires when extract left
    the receipt `extracted`, so a non-booking/low-confidence/ignored/errored
    receipt comes back with that status and a `skipped` outcome. Safe to re-run:
    a terminal receipt skips extract, and import no-ops once it's `processed`.
    The caller commits; the returned `ImportOutcome` carries the final state the
    UI needs (receipt status + placement outcome)."""
    if receipt.status in EXTRACTABLE_STATUSES:
        extract()
    return import_receipt(session, receipt)
