"""The rate tables, pinned. The trip rates match the iOS client
(outbound/TripPricing.swift); the member-day rates for people beyond five
replaced the client's per-person ones on 2026-11-04, and the client is to
follow."""

from datetime import date

import pytest

from app.services import pricing
from app.services.pricing import bracket_total, price, quote, trip_days

BASE = [
    (1, 160), (3, 480), (4, 560), (7, 800), (9, 960), (10, 1030), (14, 1310), (16, 1450),
    (17, 1500), (21, 1700), (23, 1800), (24, 1830), (30, 2010), (31, 2030), (45, 2310),
    (60, 2610), (90, 3210),
]  # fmt: skip
# Extra member-days -> what they add, all together: 10 at 20, 15 at 15,
# 15 at 10, then 5.
MEMBER_DAYS = [(1, 20), (10, 200), (11, 215), (25, 425), (26, 435), (40, 575), (41, 580), (100, 875)]
WITH_MEMBERS = [
    (3, 5, 480), (3, 6, 540), (3, 8, 660), (3, 10, 755), (3, 15, 955), (3, 20, 1080),
    (7, 6, 940), (7, 10, 1325), (14, 6, 1570), (14, 10, 2035), (30, 10, 3135), (90, 6, 4035),
]  # fmt: skip


@pytest.mark.parametrize(("days", "cents"), BASE)
def test_base_price(days, cents):
    assert price(days, 1) == cents
    assert price(days, 5) == cents  # up to five people ride on the trip's own rate


@pytest.mark.parametrize(("member_days", "cents"), MEMBER_DAYS)
def test_extra_member_days(member_days, cents):
    rates = pricing.schedule(pricing.CURRENT_SCHEDULE)
    assert bracket_total(rates.member_brackets, member_days) == cents


def test_extra_people_are_priced_by_member_day():
    # One extra person for 10 days costs what two do for 5.
    assert price(10, 6) - price(10, 5) == price(5, 7) - price(5, 5) == 200
    # And each extra person on a short trip costs less than the one before.
    added = [price(3, n) - price(3, n - 1) for n in range(6, 30)]
    assert added == sorted(added, reverse=True) and added[0] > added[-1]


@pytest.mark.parametrize(("days", "members", "cents"), WITH_MEMBERS)
def test_price_with_members(days, members, cents):
    assert price(days, members) == cents


@pytest.mark.parametrize("days", [1, 3, 4, 16, 17, 31, 90])
def test_breakdown_lines_are_contiguous_and_sum_to_the_price(days):
    q = quote(days, 8)
    assert (q.extra_members, q.member_days) == (3, 3 * days)
    for lines, span, total in (
        (q.trip_lines, days, q.base_cents),
        (q.member_lines, q.member_days, q.member_cents),
    ):
        assert lines[0].first_day == 1 and lines[-1].last_day == span
        assert all(a.last_day + 1 == b.first_day for a, b in zip(lines, lines[1:]))
        assert sum(line.cents for line in lines) == total
    assert q.total_cents == q.base_cents + q.member_cents == price(days, 8)
    assert quote(days, 5).member_lines == ()


def test_each_extra_day_costs_no_more_than_the_one_before():
    marginal = [price(d, 1) - price(d - 1, 1) if d > 1 else price(1, 1) for d in range(1, 100)]
    assert marginal == sorted(marginal, reverse=True)


def test_trip_days_counts_both_ends():
    assert trip_days(date(2026, 4, 9), date(2026, 4, 15)) == 7
    assert trip_days(date(2026, 4, 9), date(2026, 4, 9)) == 1


def test_rejects_nonsense():
    with pytest.raises(ValueError):
        price(0, 1)
    with pytest.raises(ValueError):
        price(3, 0)
    with pytest.raises(ValueError):
        price(3, 1, "1999-01")
    assert pricing.schedule(pricing.CURRENT_SCHEDULE).included_members == 5
