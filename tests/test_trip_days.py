"""The per-date scaffolding of a trip: zones, a place and a name per day."""

from sqlalchemy import func, select

from app.models import TripDay
from tests.conftest import JAPAN_TRIP


def _days(client, trip, headers):
    response = client.get(f"/trips/{trip['id']}/days", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_days_follow_the_trip_dates(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun, timezone="Asia/Tokyo")
    url = f"/trips/{trip['id']}"

    days = _days(client, trip, sun)
    assert [d["date"] for d in days] == [f"2026-04-{n:02d}" for n in range(9, 16)]
    assert [d["day_number"] for d in days] == [1, 2, 3, 4, 5, 6, 7]
    assert all(d["timezones"] == ["Asia/Tokyo"] for d in days)
    assert all(d["name"] is None and d["place"] is None for d in days)

    named = client.patch(f"{url}/days/2026-04-11", json={"name": "Wedding Day"}, headers=sun)
    assert named.status_code == 200, named.text

    # Extending adds days that take the neighbouring day's zones.
    assert client.patch(url, json={"end_date": "2026-04-17"}, headers=sun).status_code == 200
    days = _days(client, trip, sun)
    assert [d["date"] for d in days][-3:] == ["2026-04-15", "2026-04-16", "2026-04-17"]
    assert days[-1]["timezones"] == ["Asia/Tokyo"]
    assert days[-1]["day_number"] == 9

    # Shrinking removes days, and what was on them.
    assert client.patch(url, json={"start_date": "2026-04-12"}, headers=sun).status_code == 200
    days = _days(client, trip, sun)
    assert days[0]["date"] == "2026-04-12"
    assert days[0]["day_number"] == 1
    assert client.patch(url, json={"start_date": "2026-04-11"}, headers=sun).status_code == 200
    days = _days(client, trip, sun)
    assert days[0]["date"] == "2026-04-11"
    assert days[0]["name"] is None
    assert days[0]["timezones"] == ["Asia/Tokyo"]

    # Moving the whole trip keeps its zone too.
    moved = client.patch(
        url, json={"start_date": "2026-05-01", "end_date": "2026-05-03"}, headers=sun
    )
    assert moved.status_code == 200
    days = _days(client, trip, sun)
    assert [d["date"] for d in days] == ["2026-05-01", "2026-05-02", "2026-05-03"]
    assert all(d["timezones"] == ["Asia/Tokyo"] for d in days)


def test_a_trip_without_a_zone_has_empty_days(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    assert all(d["timezones"] == [] for d in _days(client, trip, sun))
    assert client.post("/trips", json={**JAPAN_TRIP, "timezone": "Asia/Tokio"}, headers=sun).status_code == 422


def test_editing_a_day(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    client.post(f"{url}/members", json={"user_id": sean.user_id}, headers=sun)
    stub = client.post("/places", json={"google_place_id": "g-flagstaff"}, headers=sun).json()
    client.patch(f"/places/{stub['id']}", json={"name": "Flagstaff"}, headers=sun)

    # Duplicate zones collapse, order is kept, the place is embedded.
    changed = client.patch(
        f"{url}/days/2026-04-10",
        json={
            "name": "Grand Canyon",
            "timezones": ["America/Phoenix", "America/Denver", "America/Phoenix"],
            "place_id": stub["id"],
        },
        headers=sun,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == "Grand Canyon"
    assert changed.json()["timezones"] == ["America/Phoenix", "America/Denver"]
    assert changed.json()["place"]["name"] == "Flagstaff"
    assert changed.json()["day_number"] == 2
    assert _days(client, trip, sun)[1]["place"]["name"] == "Flagstaff"

    # Fields can be cleared, except the zone list, which is emptied instead.
    cleared = client.patch(
        f"{url}/days/2026-04-10",
        json={"name": None, "place_id": None, "timezones": []},
        headers=sun,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["name"] is None
    assert cleared.json()["place"] is None
    assert cleared.json()["timezones"] == []

    day_url = f"{url}/days/2026-04-10"
    assert client.patch(day_url, json={"timezones": None}, headers=sun).status_code == 422
    assert client.patch(day_url, json={"timezones": ["Asia/Tokio"]}, headers=sun).status_code == 422
    assert client.patch(day_url, json={"place_id": str(trip["id"])}, headers=sun).status_code == 422
    assert client.patch(day_url, json={"colour": "red"}, headers=sun).status_code == 422
    assert client.patch(f"{url}/days/2026-04-20", json={"name": "x"}, headers=sun).status_code == 404
    assert client.patch(f"{url}/days/not-a-date", json={"name": "x"}, headers=sun).status_code == 422
    assert client.patch(day_url, json={"name": "Nope"}, headers=sean).status_code == 403
    assert client.get(f"{url}/days", headers=sean).status_code == 200


def test_deleting_a_trip_removes_its_days(client, session, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    count = select(func.count()).select_from(TripDay)
    assert session.scalar(count) == 7
    assert client.delete(f"/trips/{trip['id']}", headers=sun).status_code == 204
    assert session.scalar(count) == 0
