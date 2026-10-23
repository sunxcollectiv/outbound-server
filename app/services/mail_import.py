"""Import stage: turn an ``extracted`` receipt into trip item(s) in an existing
trip the sender is a member of, updating the receipt to ``processed``.

The trip is auto-matched by date overlap (dates-only for MVP): a unique
overlapping member-trip is used; zero or several leaves the receipt ``extracted``
with a ``no_match``/``ambiguous`` outcome (we never guess a placement). Places
are deferred — items are created with ``place_id`` null and the airport/hotel
names kept in the item ``notes``. Creating a new trip is out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.trip_items import build_trip_item
from app.models import EmailReceipt, EmailReceiptStatus, Trip, TripMember, User
from app.schemas import TripItemCreate

Outcome = Literal["placed", "no_match", "ambiguous", "skipped"]


@dataclass
class ImportOutcome:
    receipt: EmailReceipt
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


def _place_label(place: dict[str, Any] | None) -> str | None:
    if not place:
        return None
    name, iata, city = place.get("name"), place.get("iata_code"), place.get("city")
    label = name or city
    if iata and iata != name:
        label = f"{label} ({iata})" if label else iata
    return label


def _notes(item: dict[str, Any]) -> str | None:
    """Preserve the places we aren't resolving yet (no place_id) as text."""
    segments = item.get("segments") or []
    if segments:
        lines = []
        for seg in segments:
            route = " → ".join(
                x for x in (_place_label(seg.get("origin")), _place_label(seg.get("destination"))) if x
            )
            svc = " ".join(x for x in (seg.get("carrier"), seg.get("flight_number")) if x)
            lines.append(f"{svc}: {route}".strip(": ").strip() or route)
        text = "\n".join(line for line in lines if line)
    else:
        text = " → ".join(
            x for x in (_place_label(item.get("place")), _place_label(item.get("end_place"))) if x
        )
    return text or None


def _ends(part: dict[str, Any]) -> dict[str, Any]:
    return {
        "starts_at": _as_datetime(part.get("start_local")),
        "start_timezone": part.get("start_timezone"),
        "ends_at": _as_datetime(part.get("end_local")),
        "end_timezone": part.get("end_timezone"),
    }


def _item_payload(item: dict[str, Any]) -> TripItemCreate:
    kind = item.get("kind") or "activity"
    values: dict[str, Any] = {
        "kind": kind,
        "scope": "personal",
        "title": (item.get("title") or "Booking")[:200],
        "notes": _notes(item),
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
            }
            for seg in segments
        ]
    else:
        values.update(_ends(item))
        if kind == "transit":
            values["transit_mode"] = item.get("transit_mode")
    return TripItemCreate(**values)


def import_receipt(session: Session, receipt: EmailReceipt) -> ImportOutcome:
    """Place an extracted receipt's booking into the sender's matching trip.
    Only acts on `extracted` receipts; the caller commits (atomic per receipt)."""
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
    count = 0
    for item in extraction.get("items") or []:
        build_trip_item(session, trip, user, _item_payload(item))
        count += 1

    receipt.status = EmailReceiptStatus.PROCESSED
    receipt.trip_id = trip.id
    return ImportOutcome(receipt, "placed", trip_id=trip.id, item_count=count)
