import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from app.models import Place, Trip, TripItem, TripItemKind, TripItemTransitSegment
from app.schemas import (
    Itinerary,
    ItineraryDay,
    ItineraryEntry,
    LodgingStay,
    PlaceRead,
    TripItemRead,
    TripRead,
)
from app.services.google_places import place_needs_refresh
from app.services.item_visibility import present_item
from app.services.trip_groups import Links, day_owner


def _sort_key(entry: tuple[datetime, ItineraryEntry]) -> tuple[datetime, str]:
    moment, entry_read = entry
    return moment, str(entry_read.item.id)


def _layover_minutes(arriving: TripItemTransitSegment, departing: TripItemTransitSegment) -> int:
    """How long you wait between landing on one leg and leaving on the next —
    by the instants when both legs are zoned, else by the wall clocks."""
    if arriving.ends_at_utc is not None and departing.starts_at_utc is not None:
        delta = departing.starts_at_utc - arriving.ends_at_utc
    else:
        delta = departing.starts_at - arriving.ends_at
    return max(int(delta.total_seconds() // 60), 0)


def _layover_timezone(
    arriving: TripItemTransitSegment, departing: TripItemTransitSegment
) -> str | None:
    """The layover airport's IANA zone, taken from the segment zones rather than a
    place: the arriving leg's end zone, but only when the departing leg's start
    zone agrees (same airport → same zone). Null if they differ or either is
    unset — we'd rather say nothing than pick a side on inconsistent data."""
    tz = arriving.end_timezone
    return tz if tz is not None and tz == departing.start_timezone else None


def _transit_entries(
    read: TripItemRead, item: TripItem
) -> list[tuple[datetime, ItineraryEntry]]:
    """A transit booking's timeline entries: its departure, a layover for each
    stop between legs, and its arrival. A plain hop with no real arrival (a
    single leg that lands at its departure time, or never) stays one unphased
    entry, the way any other single-ended item does.

    A redacted booking shows only its own departure and arrival: its legs, and
    so its layovers, are among the details hidden."""
    if read.redacted:
        if item.ends_at is None or item.ends_at == item.starts_at:
            return [(item.starts_at, ItineraryEntry(item=read))]
        return [
            (item.starts_at, ItineraryEntry(item=read, phase="departure")),
            (item.ends_at, ItineraryEntry(item=read, phase="arrival")),
        ]

    legs = item.transit_segments
    first, last = legs[0], legs[-1]
    distinct = len(legs) > 1 or (first.ends_at is not None and first.ends_at != first.starts_at)
    if not distinct:
        return [(first.starts_at, ItineraryEntry(item=read))]

    entries = [(first.starts_at, ItineraryEntry(item=read, phase="departure"))]
    for arriving, departing in zip(legs, legs[1:]):
        if arriving.ends_at is None:  # unknown landing: no layover to show
            continue
        entries.append(
            (
                arriving.ends_at,
                ItineraryEntry(
                    item=read,
                    phase="layover",
                    layover_place=(
                        PlaceRead.model_validate(arriving.end_place)
                        if arriving.end_place is not None
                        else None
                    ),
                    layover_minutes=_layover_minutes(arriving, departing),
                    layover_timezone=_layover_timezone(arriving, departing),
                ),
            )
        )
    if last.ends_at is not None:
        entries.append((last.ends_at, ItineraryEntry(item=read, phase="arrival")))
    return entries


def _trip_places(items: list[TripItem]) -> dict[object, Place]:
    """Every distinct place reachable from a trip's items — an item's own two
    ends plus each transit leg's two ends — keyed by id to dedupe. All are
    eager-loaded by with_item_details, so this adds no queries."""
    places: dict[object, Place] = {}
    for item in items:
        candidates = [item.place, item.end_place]
        for segment in item.transit_segments:
            candidates += [segment.place, segment.end_place]
        for place in candidates:
            if place is not None:
                places[place.id] = place
    return places


def _stale_place_ids(items: list[TripItem], now: datetime) -> list[object]:
    """Ids of the trip's places whose Google cache is missing or due a refresh."""
    return [
        place.id
        for place in _trip_places(items).values()
        if place_needs_refresh(place, now=now)
    ]


def build_itinerary(
    trip: Trip,
    trips: list[Trip],
    items: list[TripItem],
    member_ids: dict[uuid.UUID, uuid.UUID],
    links: Links,
) -> Itinerary:
    """Group the items of `trips` (the caller's trips in `trip`'s group, by
    date; just `trip` when it stands alone) into calendar days by their wall
    times, each as the caller gets to see it on its own trip (see
    item_visibility). `member_ids` maps each trip to the caller's member row
    on it. Each day says which trip it belongs to: on a date two trips share,
    the parent (see trip_groups.day_owner).

    Most items appear once, on the day their start names. An item with distinct
    ends is split instead, each piece placed and sorted by the wall time of its
    own end. Transit becomes a `departure`, a `layover` at each stop between its
    legs, and an `arrival`; lodging becomes a `check_in` and a `check_out`. So a
    flight sorts by when it leaves on one day and when it lands on the next, a
    layover by when you touch down to wait, and a hotel by check-in and the last
    morning's check-out. Lodging also still shows up in each day's `lodging` list
    from check-in through check-out. Zones are ignored throughout: a day reads
    like a diary page, ordered by the clock, whichever zone each entry is in.
    """
    # (sort key, entry) so a day's list is ordered by the wall time relevant to
    # that day: a flight arriving at 11:00 sorts by 11:00, not by when it left.
    items_by_day: dict[date, list[tuple[datetime, ItineraryEntry]]] = defaultdict(list)
    lodging_by_day: dict[date, list[LodgingStay]] = defaultdict(list)
    detailed: list[TripItem] = []  # the items whose places the caller may see

    for item in items:
        start_day = item.starts_at.date()
        read = present_item(item, member_ids[item.trip_id])
        if not read.redacted:
            detailed.append(item)

        if item.kind is TripItemKind.TRANSIT:
            for moment, entry in _transit_entries(read, item):
                items_by_day[moment.date()].append((moment, entry))
        elif item.kind is TripItemKind.LODGING and item.ends_at is not None and (
            item.ends_at != item.starts_at
        ):
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

    # Widen past the trips' own dates rather than silently dropping items that
    # fall outside them (e.g. after a trip was shortened).
    populated = [*items_by_day, *lodging_by_day]
    journey_start = min(t.start_date for t in trips)
    first = min([journey_start, *populated])
    last = max([*(t.end_date for t in trips), *populated])

    days = []
    day = first
    while day <= last:
        owner = day_owner(day, trips, links)
        covering = [t.id for t in trips if t.start_date <= day <= t.end_date]
        lodging = lodging_by_day.get(day, [])
        day_items = [entry for _, entry in sorted(items_by_day.get(day, []), key=_sort_key)]
        days.append(
            ItineraryDay(
                date=day,
                day_number=(day - journey_start).days + 1,
                trip_id=owner.id if owner is not None else None,
                trip_ids=covering,
                is_gap=not covering and not lodging and not day_items,
                lodging=lodging,
                items=day_items,
            )
        )
        day += timedelta(days=1)

    return Itinerary(
        trip=TripRead.model_validate(trip),
        trips=[TripRead.model_validate(t) for t in trips],
        days=days,
        stale_place_ids=_stale_place_ids(detailed, datetime.now(UTC)),
    )
