from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.models import Trip, TripItem, TripItemKind
from app.schemas import Itinerary, ItineraryDay, LodgingStay, TripItemRead, TripRead


def _item_timezone(item: TripItem, trip: Trip) -> ZoneInfo:
    if item.place is not None and item.place.timezone:
        return ZoneInfo(item.place.timezone)
    return ZoneInfo(trip.timezone)


def build_itinerary(trip: Trip, items: list[TripItem]) -> Itinerary:
    """Group a trip's items into calendar days, in local time.

    Every item appears once, on the day it starts. Lodging additionally shows
    up in each day's `lodging` list from check-in through check-out.
    """
    items_by_day: dict[date, list[TripItemRead]] = defaultdict(list)
    lodging_by_day: dict[date, list[LodgingStay]] = defaultdict(list)

    for item in sorted(items, key=lambda i: i.starts_at):
        tz = _item_timezone(item, trip)
        start_day = item.starts_at.astimezone(tz).date()
        read = TripItemRead.model_validate(item)
        items_by_day[start_day].append(read)

        if item.kind is TripItemKind.LODGING:
            end_day = item.ends_at.astimezone(tz).date() if item.ends_at else start_day
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
                items=items_by_day.get(day, []),
            )
        )
        day += timedelta(days=1)

    return Itinerary(trip=TripRead.model_validate(trip), days=days)
