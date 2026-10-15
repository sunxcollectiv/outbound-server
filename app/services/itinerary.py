from collections import defaultdict
from datetime import date, datetime, timedelta

from app.models import Trip, TripItem, TripItemKind
from app.schemas import Itinerary, ItineraryDay, LodgingStay, TripItemRead, TripRead
from app.services.timekeeping import to_utc


def _day_zone(trip: Trip, day: date) -> str:
    """The zone a floating time on `day` is read in for ordering: the trip
    day's first zone, else UTC. A client would make the same choice."""
    for trip_day in trip.days:
        if trip_day.date == day and trip_day.timezones:
            return trip_day.timezones[0]
    return "UTC"


def _sort_key(entry: tuple[datetime, TripItemRead]) -> tuple[datetime, str]:
    moment, read = entry
    return moment, str(read.id)


def build_itinerary(trip: Trip, items: list[TripItem]) -> Itinerary:
    """Group a trip's items into calendar days, in local time.

    Every item appears on the day it starts, in its own zone. Transit also
    appears on the day it arrives when that's a different day (in the
    destination's zone), so both the departure and the arrival are visible; the
    item carries both times and the client decides which to emphasise. Lodging
    additionally shows up in each day's `lodging` list from check-in through
    check-out.
    """
    # (sort key, item) so a day's list is ordered by the moment relevant to that
    # day: a flight arriving at 11:00 sorts by 11:00, not by when it took off.
    # Keys are instants, the true order on a day that crosses zones; a floating
    # time is read in the day's zone for this purpose only.
    items_by_day: dict[date, list[tuple[datetime, TripItemRead]]] = defaultdict(list)
    lodging_by_day: dict[date, list[LodgingStay]] = defaultdict(list)

    for item in items:
        start_day = item.starts_at.date()
        read = TripItemRead.model_validate(item)
        start_key = item.starts_at_utc or to_utc(item.starts_at, _day_zone(trip, start_day))
        items_by_day[start_day].append((start_key, read))

        if item.kind is TripItemKind.TRANSIT and item.ends_at is not None:
            arrival_day = item.ends_at.date()
            if arrival_day != start_day:
                end_key = item.ends_at_utc or to_utc(item.ends_at, _day_zone(trip, arrival_day))
                items_by_day[arrival_day].append((end_key, read))

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
                items=[read for _, read in sorted(items_by_day.get(day, []), key=_sort_key)],
            )
        )
        day += timedelta(days=1)

    return Itinerary(trip=TripRead.model_validate(trip), days=days)
