from datetime import date, timedelta

from app.models import Trip, TripDay


def sync_trip_days(trip: Trip, seed_timezone: str | None = None) -> None:
    """Make trip.days exactly one row per date from start_date to end_date.

    Days that fell outside the dates are removed, their name and place with
    them. New days copy their zones from the nearest day the trip had before
    (the old first day for dates before it, the old last day for dates after),
    so extending or shifting a trip keeps it in the same zone; name and place
    aren't copied. A trip with no days yet seeds each with `seed_timezone`.
    """
    before = {day.date: day for day in trip.days}
    first, last = (min(before), max(before)) if before else (None, None)

    for day in list(trip.days):
        if not trip.start_date <= day.date <= trip.end_date:
            trip.days.remove(day)

    wanted = trip.start_date
    while wanted <= trip.end_date:
        if wanted not in before:
            if before:
                source = before[first] if wanted < first else before[last]
                timezones = list(source.timezones)
            else:
                timezones = [seed_timezone] if seed_timezone else []
            trip.days.append(TripDay(date=wanted, timezones=timezones))
        wanted += timedelta(days=1)


def day_for(trip: Trip, on: date) -> TripDay | None:
    return next((day for day in trip.days if day.date == on), None)
