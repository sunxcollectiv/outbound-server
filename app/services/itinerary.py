from collections import defaultdict
from datetime import date, datetime, timedelta

from app.models import Trip, TripItem, TripItemKind
from app.schemas import (
    Itinerary,
    ItineraryDay,
    ItineraryEntry,
    LodgingStay,
    TripItemRead,
    TripRead,
)


def _sort_key(entry: tuple[datetime, ItineraryEntry]) -> tuple[datetime, str]:
    moment, entry_read = entry
    return moment, str(entry_read.item.id)


def build_itinerary(trip: Trip, items: list[TripItem]) -> Itinerary:
    """Group a trip's items into calendar days by their wall times.

    Most items appear once, on the day their start names. An item with a distinct
    second end is split into two entries instead, one per end, each placed and
    sorted by the wall time of that end: transit into a `departure` and an
    `arrival`, lodging into a `check_in` and a `check_out`. So a flight sorts by
    when it leaves on one day and by when it lands on the other, and a hotel sorts
    by check-in on arrival day and by check-out on the last morning. Lodging also
    still shows up in each day's `lodging` list from check-in through check-out.
    Zones are ignored throughout: a day reads like a diary page, ordered by the
    clock, whichever zone each entry is in.
    """
    # (sort key, entry) so a day's list is ordered by the wall time relevant to
    # that day: a flight arriving at 11:00 sorts by 11:00, not by when it left.
    items_by_day: dict[date, list[tuple[datetime, ItineraryEntry]]] = defaultdict(list)
    lodging_by_day: dict[date, list[LodgingStay]] = defaultdict(list)

    for item in items:
        start_day = item.starts_at.date()
        read = TripItemRead.model_validate(item)

        splits_into_ends = item.ends_at is not None and item.ends_at != item.starts_at
        if item.kind is TripItemKind.TRANSIT and splits_into_ends:
            items_by_day[start_day].append(
                (item.starts_at, ItineraryEntry(item=read, phase="departure"))
            )
            items_by_day[item.ends_at.date()].append(
                (item.ends_at, ItineraryEntry(item=read, phase="arrival"))
            )
        elif item.kind is TripItemKind.LODGING and splits_into_ends:
            items_by_day[start_day].append(
                (item.starts_at, ItineraryEntry(item=read, phase="check_in"))
            )
            items_by_day[item.ends_at.date()].append(
                (item.ends_at, ItineraryEntry(item=read, phase="check_out"))
            )
        else:
            items_by_day[start_day].append((item.starts_at, ItineraryEntry(item=read)))

        if item.kind is TripItemKind.LODGING:
            end_day = item.ends_at.date() if item.ends_at else start_day
            day = start_day
            while day <= end_day:
                if day == start_day:
                    phase = "check_in"
                elif day == end_day:
                    phase = "check_out"
                else:
                    phase = "staying"
                lodging_by_day[day].append(LodgingStay(item=read, phase=phase))
                day += timedelta(days=1)

    # Widen past the trip's own dates rather than silently dropping items that
    # fall outside them (e.g. after the trip was shortened).
    populated = [*items_by_day, *lodging_by_day]
    first = min([trip.start_date, *populated])
    last = max([trip.end_date, *populated])

    days = []
    day = first
    while day <= last:
        days.append(
            ItineraryDay(
                date=day,
                day_number=(day - trip.start_date).days + 1,
                lodging=lodging_by_day.get(day, []),
                items=[entry for _, entry in sorted(items_by_day.get(day, []), key=_sort_key)],
            )
        )
        day += timedelta(days=1)

    return Itinerary(trip=TripRead.model_validate(trip), days=days)
