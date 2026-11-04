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

Nothing here commits; the caller owns the transaction. Every row is appended
to its user's chain (see CreditEntry), which carries the running balance, so
a balance is one row read, not a sum. Writers lock first: `settle` the trip's
row, then the payer's and owner's users (in id order), so two edits of one
trip or two writes to one balance can't interleave. The chain's constraints
back that up: a write that slipped past the lock fails instead of spending
the same balance twice, and no balance can go negative.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CreditEntry, CreditEntryKind, Trip, TripMember, User
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
    """The running balance on the user's newest row; 0 before their first."""
    return (
        session.scalar(
            select(CreditEntry.balance_after_cents)
            .where(CreditEntry.user_id == user_id)
            .order_by(CreditEntry.seq.desc())
            .limit(1)
        )
        or 0
    )


def lock_users(session: Session, *user_ids: uuid.UUID) -> None:
    """Locks these users' balances until commit: nothing else can add to
    their ledgers meanwhile. In id order, so two transactions locking
    overlapping sets of people can't deadlock."""
    session.execute(
        select(User.id).where(User.id.in_(set(user_ids))).order_by(User.id).with_for_update()
    )


def _append(session: Session, entry: CreditEntry) -> CreditEntry:
    """Adds `entry` to the end of its user's chain (see CreditEntry). The
    caller must hold the user's lock (lock_users)."""
    previous = session.execute(
        select(CreditEntry.id, CreditEntry.balance_after_cents)
        .where(CreditEntry.user_id == entry.user_id)
        .order_by(CreditEntry.seq.desc())
        .limit(1)
    ).first()
    if previous is not None:
        entry.previous_id, entry.previous_balance_cents = previous
    entry.balance_after_cents = (entry.previous_balance_cents or 0) + entry.amount_cents
    session.add(entry)
    session.flush()
    return entry


@dataclass
class StatementLine:
    """One ledger row as its owner sees it: the row itself (its columns read
    straight through) and what it connects to.
    `trip` is the trip as it is now, only while it exists and the owner is
    still on it. On a refund, `refund_of_payer` is whoever paid the charge it
    draws down; on a charge, `refunded_cents` and `refunded_to` say how much
    of it has come back, and to whom (the trip's owner, who may be someone
    else)."""

    entry: CreditEntry
    trip: Trip | None = None
    refund_of_payer: User | None = None
    refunded_cents: int = 0
    refunded_to: list[User] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.entry, name)


def statement(
    session: Session, user_id: uuid.UUID, *, limit: int, before: CreditEntry | None = None
) -> tuple[list[StatementLine], bool]:
    """A page of the user's ledger, newest first, starting after `before`
    (one of their rows). Returns the page and whether there's more."""
    query = (
        select(CreditEntry)
        .where(CreditEntry.user_id == user_id)
        .order_by(CreditEntry.seq.desc())
    )
    if before is not None:
        query = query.where(CreditEntry.seq < before.seq)
    entries = list(session.scalars(query.limit(limit + 1)))
    return _lines(session, user_id, entries[:limit]), len(entries) > limit


def statement_line(session: Session, entry: CreditEntry) -> StatementLine:
    """One row of its owner's statement, e.g. a grant just made."""
    return _lines(session, entry.user_id, [entry])[0]


def _lines(
    session: Session, user_id: uuid.UUID, entries: list[CreditEntry]
) -> list[StatementLine]:
    """Fills in what each row connects to, a few queries for the whole page."""
    trips: dict[uuid.UUID, Trip] = {}
    if trip_ids := {e.trip_id for e in entries if e.trip_id is not None}:
        trips = {
            trip.id: trip
            for trip in session.scalars(
                select(Trip)
                .join(TripMember, TripMember.trip_id == Trip.id)
                .where(Trip.id.in_(trip_ids), TripMember.user_id == user_id)
            )
        }

    payers: dict[uuid.UUID, User] = {}
    if charge_ids := {e.refund_of_id for e in entries if e.refund_of_id is not None}:
        payers = dict(
            session.execute(
                select(CreditEntry.id, User)
                .join(User, User.id == CreditEntry.user_id)
                .where(CreditEntry.id.in_(charge_ids))
            ).all()
        )

    refunded: dict[uuid.UUID, int] = defaultdict(int)
    refunded_to: dict[uuid.UUID, dict[uuid.UUID, User]] = defaultdict(dict)
    if own_charges := [e.id for e in entries if e.kind == CreditEntryKind.TRIP_CHARGE]:
        refunds = session.execute(
            select(CreditEntry.refund_of_id, CreditEntry.amount_cents, User)
            .outerjoin(User, User.id == CreditEntry.user_id)
            .where(CreditEntry.refund_of_id.in_(own_charges))
            .order_by(CreditEntry.seq)
        )
        for charge_id, cents, recipient in refunds:
            refunded[charge_id] += cents
            if recipient is not None:
                refunded_to[charge_id].setdefault(recipient.id, recipient)

    return [
        StatementLine(
            entry=entry,
            trip=trips.get(entry.trip_id) if entry.trip_id is not None else None,
            refund_of_payer=payers.get(entry.refund_of_id) if entry.refund_of_id else None,
            refunded_cents=refunded.get(entry.id, 0),
            refunded_to=list(refunded_to.get(entry.id, {}).values()),
        )
        for entry in entries
    ]


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
    lock_users(session, user_id)
    return _append(
        session,
        CreditEntry(
            user_id=user_id,
            kind=kind,
            amount_cents=amount_cents,
            description=description,
            external_id=external_id,
        ),
    )


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
    # Locks, in a fixed order: the trip, then everyone whose ledger this may
    # write to. The first also flushes a brand-new trip so its row exists.
    session.execute(select(Trip.id).where(Trip.id == trip.id).with_for_update())
    lock_users(session, payer.id, trip.owner_id)
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
        _append(
            session,
            CreditEntry(
                user_id=payer.id,
                trip_id=trip.id,
                kind=CreditEntryKind.TRIP_CHARGE,
                amount_cents=-outcome.price_cents,
                description=trip.title,
            ),
        )
    elif outcome.refund_cents > 0:
        _refund(session, trip, outcome.refund_cents)
    return outcome


def refund_all(session: Session, trip: Trip, *, now: datetime) -> int:
    """Everything paid for the trip goes back to its owner, if the window is
    still open. For deleting a trip. Returns what was refunded."""
    session.execute(select(Trip.id).where(Trip.id == trip.id).with_for_update())
    if not refund_window_open(trip, now):
        return 0
    paid = paid_for(session, trip.id)
    if paid > 0:
        lock_users(session, trip.owner_id)
        _refund(session, trip, paid)
    return paid


def _refund(session: Session, trip: Trip, cents: int) -> None:
    """Give `cents` back to the trip's owner, as one row per charge drawn
    down, most recent charge first. The money all goes to the owner; the
    rows say whose charge each piece came out of. The caller must hold the
    owner's lock."""
    refunded = (
        select(CreditEntry.refund_of_id, func.sum(CreditEntry.amount_cents).label("cents"))
        .where(CreditEntry.trip_id == trip.id, CreditEntry.refund_of_id.is_not(None))
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
        _append(
            session,
            CreditEntry(
                user_id=trip.owner_id,
                trip_id=trip.id,
                kind=CreditEntryKind.TRIP_REFUND,
                amount_cents=take,
                refund_of_id=charge.id,
                description=trip.title,
            ),
        )
        left -= take
        if left == 0:
            break
    if left:
        raise CreditsError("Refund exceeds what was paid")
