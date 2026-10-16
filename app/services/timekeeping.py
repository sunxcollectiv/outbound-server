"""Wall-clock time in a named zone, and the instant it stands for.

Trip item times are stored as naive local wall time plus an IANA zone name:
that's what a ticket or a booking says, and it stays right even if the zone's
rules change later. The matching UTC instant is derived from the pair whenever
something needs to order or compare items.

`zoneinfo` resolves awkward wall times on its own: a time in a DST gap (02:30
on the spring-forward night) is pushed forward, and an ambiguous time in the
fall-back hour takes the first occurrence (fold=0). Neither is an error here.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:  # ZoneInfo raises several unrelated types for bad keys
        return False
    return True


def localize(naive: datetime, tz: str) -> datetime:
    """The aware moment that `naive` names in `tz`."""
    if naive.tzinfo is not None:
        raise ValueError("expected a naive local datetime")
    return naive.replace(tzinfo=ZoneInfo(tz))


def to_utc(naive: datetime, tz: str) -> datetime:
    """The UTC instant that `naive` names in `tz`."""
    return localize(naive, tz).astimezone(UTC)


def to_local(value: datetime, tz: str) -> datetime:
    """Naive wall time in `tz`. A naive value is already local and passes
    through untouched; an aware one is converted into `tz` first."""
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
