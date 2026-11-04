"""The rate tables, pinned to the numbers the iOS client shipped with
(outbound/TripPricing.swift) so the two can never disagree."""

from datetime import date

import pytest

from app.services import pricing
from app.services.pricing import price, quote, trip_days

BASE = [
    (1, 160), (3, 480), (4, 560), (7, 800), (9, 960), (10, 1030), (14, 1310), (16, 1450),
    (17, 1500), (21, 1700), (23, 1800), (24, 1830), (30, 2010), (31, 2030), (45, 2310),
    (60, 2610), (90, 3210),
]  # fmt: skip
EXTRA_MEMBER = [(1, 20), (7, 140), (9, 170), (14, 245), (30, 405), (31, 410), (90, 705)]
WITH_MEMBERS = [
    (3, 5, 480), (3, 6, 540), (3, 10, 780), (7, 8, 1220), (9, 6, 1130), (10, 10, 1955),
    (14, 7, 1800), (16, 9, 2510), (30, 10, 4035), (45, 6, 2790), (90, 10, 6735),
]  # fmt: skip


@pytest.mark.parametrize(("days", "cents"), BASE)
def test_base_price(days, cents):
    assert price(days, 1) == cents
    assert price(days, 5) == cents  # up to five people ride on the trip's own rate


@pytest.mark.parametrize(("days", "cents"), EXTRA_MEMBER)
def test_each_extra_member(days, cents):
    assert quote(days, 6).per_extra_member_cents == cents
    assert price(days, 6) - price(days, 5) == cents


@pytest.mark.parametrize(("days", "members", "cents"), WITH_MEMBERS)
def test_price_with_members(days, members, cents):
    assert price(days, members) == cents


@pytest.mark.parametrize("days", [1, 3, 4, 16, 17, 31, 90])
def test_breakdown_lines_are_contiguous_and_sum_to_the_price(days):
    q = quote(days, 8)
    for lines, total in ((q.trip_lines, q.base_cents), (q.member_lines, q.per_extra_member_cents)):
        assert lines[0].first_day == 1 and lines[-1].last_day == days
        assert all(a.last_day + 1 == b.first_day for a, b in zip(lines, lines[1:]))
        assert sum(line.cents for line in lines) == total
    assert q.extra_members == 3
    assert q.total_cents == q.base_cents + 3 * q.per_extra_member_cents


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
