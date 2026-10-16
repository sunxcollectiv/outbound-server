from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models.base import LocalDateTime
from app.services.timekeeping import is_valid_timezone, localize, to_local, to_utc

JST = timezone(timedelta(hours=9))


def test_localize_rejects_aware():
    with pytest.raises(ValueError):
        localize(datetime(2026, 4, 14, 9, tzinfo=JST), "Asia/Tokyo")


def test_to_local_converts_aware_into_zone():
    # 09:00 JST is 17:00 the previous day in Los Angeles.
    local = to_local(datetime(2026, 4, 14, 9, tzinfo=JST), "America/Los_Angeles")
    assert local == datetime(2026, 4, 13, 17)
    assert local.tzinfo is None


def test_to_local_keeps_naive():
    naive = datetime(2026, 4, 14, 9)
    assert to_local(naive, "America/Los_Angeles") is naive


def test_to_utc_round_trips():
    instant = to_utc(datetime(2026, 4, 14, 9), "Asia/Tokyo")
    assert instant == datetime(2026, 4, 14, 0, tzinfo=UTC)
    assert instant.tzinfo is UTC
    assert to_local(instant, "Asia/Tokyo") == datetime(2026, 4, 14, 9)


def test_dst_gap_and_fold_are_normalised():
    # 02:30 doesn't exist in Los Angeles on 2026-03-08; it lands at 10:30 UTC
    # (03:30 PDT) rather than raising.
    gap = to_utc(datetime(2026, 3, 8, 2, 30), "America/Los_Angeles")
    assert gap == datetime(2026, 3, 8, 10, 30, tzinfo=UTC)
    # 01:30 happens twice on 2026-11-01; the first (PDT) occurrence wins.
    fold = to_utc(datetime(2026, 11, 1, 1, 30), "America/Los_Angeles")
    assert fold == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)


def test_bad_zone_names():
    assert is_valid_timezone("Asia/Tokyo")
    assert not is_valid_timezone("Asia/Tokio")
    assert not is_valid_timezone("")
    with pytest.raises(Exception):
        to_utc(datetime(2026, 4, 14, 9), "Asia/Tokio")


def test_local_datetime_column_rejects_aware_values():
    column = LocalDateTime()
    naive = datetime(2026, 4, 14, 9)
    assert column.process_bind_param(naive, None) is naive
    assert column.process_bind_param(None, None) is None
    with pytest.raises(ValueError):
        column.process_bind_param(datetime(2026, 4, 14, 9, tzinfo=JST), None)
