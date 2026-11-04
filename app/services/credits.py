"""Credits: a user's balance, and settling a trip against it.

A trip is settled, not charged in deltas: what it owes is a pure function of
its current length, roster and schedule (app.services.pricing); what has been
paid is the net of its ledger rows. Every write to a trip settles the
difference. More owed than paid: the person making the change pays it from
their balance, or the write fails. Less owed than paid: the difference is
refunded to the trip's owner, whoever paid (so a co-organizer who chipped in
a dollar doesn't walk off with the whole refund when the trip is cancelled;
squaring that up between them is theirs to do, for now). Each refund row
still records which charge it draws down, most recent first, so who paid
what and how much of it came back is always on record. Refunds stop at the
cutoff, 24h after the trip starts; after that, cutting a trip down is free
but gives nothing back.

Nothing here commits; the caller owns the transaction. `settle` locks the
trip's row and then the payer's, in that order, so two edits of one trip or
two debits of one balance can't interleave and a balance can't go negative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CreditEntry, CreditEntryKind, Trip, User
from app.services.pricing import Quote, quote, trip_days

REFUND_GRACE = timedelta(hours=24)


class CreditsError(Exception):
    pass


class InsufficientCredits(CreditsError):
    def __init__(self, price_cents: int, balance_cents: int) -> None:
        super().__init__("Not enough credits")
        self.price_cents = price_cents
        self.balance_cents = balance_cents

    @property
    def shortfall_cents(self) -> int:
        return self.price_cents - self.balance_cents


class PriceChanged(CreditsError):
    def __init__(self, expected_price_cents: int, price_cents: int) -> None:
        super().__init__("Price changed since it was quoted")
        self.expected_price_cents = expected_price_cents
        self.price_cents = price_cents


@dataclass(frozen=True)
class Settlement:
    """What settling a trip would do (or did)."""

    quote: Quote
    owed_cents: int  # the trip's price as it stands
    paid_cents: int  # net of its charges and refunds so far
    refund_window_open: bool

    @property
    def price_cents(self) -> int:
        """What the payer is charged now."""
        return max(self.owed_cents - self.paid_cents, 0)

    @property
    def refund_cents(self) -> int:
        """What goes back to the trip's owner now."""
        if not self.refund_window_open:
            return 0
        return max(self.paid_cents - self.owed_cents, 0)


def refund_cutoff(trip: Trip) -> datetime:
    """Trips carry no timezone, so the day starts at UTC midnight."""
    return datetime.combine(trip.start_date, time.min, tzinfo=UTC) + REFUND_GRACE


def refund_window_open(trip: Trip, now: datetime) -> bool:
    return now < refund_cutoff(trip)


def balance(session: Session, user_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.coalesce(func.sum(CreditEntry.amount_cents), 0)).where(
            CreditEntry.user_id == user_id
        )
    )


def ledger(session: Session, user_id: uuid.UUID) -> list[CreditEntry]:
    """Newest first."""
    return list(
        session.scalars(
            select(CreditEntry)
            .where(CreditEntry.user_id == user_id)
            .order_by(CreditEntry.seq.desc())
        )
    )


def paid_for(session: Session, trip_id: uuid.UUID) -> int:
    """Net spent on a trip so far: charges less refunds, as a positive number."""
    return -session.scalar(
        select(func.coalesce(func.sum(CreditEntry.amount_cents), 0)).where(
            CreditEntry.trip_id == trip_id
        )
    )


def grant(
    session: Session,
    user_id: uuid.UUID,
    amount_cents: int,
    *,
    kind: CreditEntryKind = CreditEntryKind.GRANT,
    description: str | None = None,
    external_id: str | None = None,
) -> CreditEntry:
    entry = CreditEntry(
        user_id=user_id,
        kind=kind,
        amount_cents=amount_cents,
        description=description,
        external_id=external_id,
    )
    session.add(entry)
    session.flush()
    return entry


def settlement_for(
    session: Session, trip: Trip, *, days: int, members: int, now: datetime
) -> Settlement:
    """What settling would do for a trip of `days` and `members`, which may
    differ from the trip's current state (a quote for an edit). Reads only."""
    q = quote(days, members, trip.price_schedule)
    return Settlement(
        quote=q,
        owed_cents=q.total_cents,
        paid_cents=paid_for(session, trip.id) if trip.id is not None else 0,
        refund_window_open=refund_window_open(trip, now),
    )


def settle(
    session: Session,
    trip: Trip,
    payer: User,
    *,
    now: datetime,
    expected_price_cents: int | None = None,
) -> Settlement:
    """Bring the trip's payments in line with its price, as it stands in the
    session (call after applying the edit, before committing). Charges the
    payer, or refunds the owner, as needed. With `expected_price_cents`,
    refuses if the charge isn't what the caller was quoted."""
    # Locks, in a fixed order. The first also flushes a brand-new trip so its
    # row exists for the ledger's foreign key.
    session.execute(select(Trip.id).where(Trip.id == trip.id).with_for_update())
    session.execute(select(User.id).where(User.id == payer.id).with_for_update())
    outcome = settlement_for(
        session,
        trip,
        days=trip_days(trip.start_date, trip.end_date),
        members=len(trip.members),
        now=now,
    )
    if expected_price_cents is not None and expected_price_cents != outcome.price_cents:
        raise PriceChanged(expected_price_cents, outcome.price_cents)
    if outcome.price_cents > 0:
        available = balance(session, payer.id)
        if available < outcome.price_cents:
            raise InsufficientCredits(outcome.price_cents, available)
        session.add(
            CreditEntry(
                user_id=payer.id,
                trip_id=trip.id,
                kind=CreditEntryKind.TRIP_CHARGE,
                amount_cents=-outcome.price_cents,
                description=trip.title,
            )
        )
    elif outcome.refund_cents > 0:
        _refund(session, trip, outcome.refund_cents)
    session.flush()
    return outcome


def refund_all(session: Session, trip: Trip, *, now: datetime) -> int:
    """Everything paid for the trip goes back to its owner, if the window is
    still open. For deleting a trip. Returns what was refunded."""
    session.execute(select(Trip.id).where(Trip.id == trip.id).with_for_update())
    if not refund_window_open(trip, now):
        return 0
    paid = paid_for(session, trip.id)
    if paid > 0:
        _refund(session, trip, paid)
        session.flush()
    return paid


def _refund(session: Session, trip: Trip, cents: int) -> None:
    """Give `cents` back to the trip's owner, as one row per charge drawn
    down, most recent charge first. The money all goes to the owner; the
    rows say whose charge each piece came out of."""
    refunded = (
        select(CreditEntry.refund_of_id, func.sum(CreditEntry.amount_cents).label("cents"))
        .where(CreditEntry.refund_of_id.is_not(None))
        .group_by(CreditEntry.refund_of_id)
        .subquery()
    )
    charges = session.execute(
        select(CreditEntry, func.coalesce(refunded.c.cents, 0))
        .outerjoin(refunded, refunded.c.refund_of_id == CreditEntry.id)
        .where(CreditEntry.trip_id == trip.id, CreditEntry.kind == CreditEntryKind.TRIP_CHARGE)
        .order_by(CreditEntry.seq.desc())
    ).all()
    left = cents
    for charge, already in charges:
        take = min(-charge.amount_cents - already, left)
        if take <= 0:
            continue
        session.add(
            CreditEntry(
                user_id=trip.owner_id,
                trip_id=trip.id,
                kind=CreditEntryKind.TRIP_REFUND,
                amount_cents=take,
                refund_of_id=charge.id,
                description=trip.title,
            )
        )
        left -= take
        if left == 0:
            break
    if left:
        raise CreditsError("Refund exceeds what was paid")
