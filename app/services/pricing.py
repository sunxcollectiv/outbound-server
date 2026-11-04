"""What a trip costs, by its length and size. Pure arithmetic, no database.

Each day is charged at the rate of the bracket it falls in, like tax
brackets: the first 3 days at $1.60, the next 6 at $0.80, and so on, down to
$0.20 a day after day 30. That covers up to `included_members` people, owner
included; each person beyond adds their own daily rate, from $0.20 down to
$0.05. Amounts are whole USD cents, so nothing is ever rounded.

Rates are versioned: each trip pins the schedule key it was created under
(trips.price_schedule), so changing the rates later never reprices a trip
that's already paid for. Keys are only ever added to SCHEDULES.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Bracket:
    first_day: int
    last_day: int | None  # None: open-ended
    cents_per_day: int


@dataclass(frozen=True)
class Schedule:
    key: str
    # How many people the trip's own rate covers, owner included.
    included_members: int
    trip_brackets: tuple[Bracket, ...]
    # Per person beyond included_members, per trip day.
    member_brackets: tuple[Bracket, ...]


@dataclass(frozen=True)
class BracketLine:
    """One row of a price breakdown: the days of a trip that fell in one bracket."""

    first_day: int
    last_day: int
    cents_per_day: int

    @property
    def days(self) -> int:
        return self.last_day - self.first_day + 1

    @property
    def cents(self) -> int:
        return self.days * self.cents_per_day


@dataclass(frozen=True)
class Quote:
    schedule: str
    days: int
    members: int
    extra_members: int
    trip_lines: tuple[BracketLine, ...]
    member_lines: tuple[BracketLine, ...]

    @property
    def base_cents(self) -> int:
        return sum(line.cents for line in self.trip_lines)

    @property
    def per_extra_member_cents(self) -> int:
        return sum(line.cents for line in self.member_lines)

    @property
    def total_cents(self) -> int:
        return self.base_cents + self.extra_members * self.per_extra_member_cents


CURRENT_SCHEDULE = "2026-11"

SCHEDULES: dict[str, Schedule] = {
    "2026-11": Schedule(
        key="2026-11",
        included_members=5,
        trip_brackets=(
            Bracket(1, 3, 160),
            Bracket(4, 9, 80),
            Bracket(10, 16, 70),
            Bracket(17, 23, 50),
            Bracket(24, 30, 30),
            Bracket(31, None, 20),
        ),
        member_brackets=(
            Bracket(1, 7, 20),
            Bracket(8, 14, 15),
            Bracket(15, 30, 10),
            Bracket(31, None, 5),
        ),
    ),
}


def schedule(key: str) -> Schedule:
    try:
        return SCHEDULES[key]
    except KeyError:
        raise ValueError(f"Unknown price schedule: {key}") from None


def trip_days(start: date, end: date) -> int:
    """Both ends count: a trip from the 9th to the 15th is 7 days."""
    return (end - start).days + 1


def bracket_lines(brackets: Sequence[Bracket], days: int) -> tuple[BracketLine, ...]:
    return tuple(
        BracketLine(b.first_day, min(days, b.last_day or days), b.cents_per_day)
        for b in brackets
        if days >= b.first_day
    )


def bracket_total(brackets: Sequence[Bracket], days: int) -> int:
    return sum(line.cents for line in bracket_lines(brackets, days))


def quote(days: int, members: int, schedule_key: str = CURRENT_SCHEDULE) -> Quote:
    if days < 1:
        raise ValueError("A trip is at least one day")
    if members < 1:
        raise ValueError("A trip has at least its owner")
    rates = schedule(schedule_key)
    return Quote(
        schedule=rates.key,
        days=days,
        members=members,
        extra_members=max(members - rates.included_members, 0),
        trip_lines=bracket_lines(rates.trip_brackets, days),
        member_lines=bracket_lines(rates.member_brackets, days),
    )


def price(days: int, members: int, schedule_key: str = CURRENT_SCHEDULE) -> int:
    """price = trip brackets over the days + (members beyond the included
    count) × member brackets over the days."""
    return quote(days, members, schedule_key).total_cents
