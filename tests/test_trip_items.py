import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models import TripItem


def _place(client, headers, **fields):
    """Register a stub from a Google id, then fill in the given fields (name,
    timezone, ...) the way the later enrichment/review step would."""
    stub = client.post(
        "/places", json={"google_place_id": f"g-{fields['name']}"}, headers=headers
    )
    assert stub.status_code == 201, stub.text
    enriched = client.patch(f"/places/{stub.json()['id']}", json=fields, headers=headers)
    assert enriched.status_code == 200, enriched.text
    return enriched.json()


def test_item_round_trip_with_places(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    kyoto = _place(client, sun, name="Kyoto Station", timezone="Asia/Tokyo")
    osaka = _place(client, sun, name="Shin-Osaka Station", timezone="Asia/Tokyo")

    # Wall times only; both zones come from the places.
    created = client.post(
        url,
        json={
            "kind": "transit",
            "scope": "group",
            "title": "Train to Osaka",
            "tags": ["Transit", "JR"],
            "starts_at": "2026-04-14T09:00:00",
            "ends_at": "2026-04-14T09:30:00",
            "place_id": kyoto["id"],
            "end_place_id": osaka["id"],
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["place"]["name"] == "Kyoto Station"
    assert item["end_place"]["name"] == "Shin-Osaka Station"
    assert item["starts_at"] == "2026-04-14T09:00:00"
    assert item["start_timezone"] == "Asia/Tokyo"
    assert item["ends_at"] == "2026-04-14T09:30:00"
    assert item["end_timezone"] == "Asia/Tokyo"
    assert item["starts_at_utc"] == "2026-04-14T00:00:00Z"
    assert item["ends_at_utc"] == "2026-04-14T00:30:00Z"
    assert item["tags"] == ["Transit", "JR"]
    assert item["requirement"] == "optional"
    assert item["visibility"] is None
    assert item["created_by"]["email"] == "sun@example.com"
    assert item["participants"] == []

    # Changing the place id is reflected in the embedded place.
    patched = client.patch(
        f"{url}/{item['id']}", json={"place_id": osaka["id"], "notes": "Car 5"}, headers=sun
    )
    assert patched.status_code == 200
    assert patched.json()["place"]["name"] == "Shin-Osaka Station"
    assert patched.json()["notes"] == "Car 5"

    assert client.delete(f"{url}/{item['id']}", headers=sun).status_code == 204
    assert client.get(f"{url}/{item['id']}", headers=sun).status_code == 404


def test_item_validation(client, make_user, make_trip):
    sun, stranger = make_user("sun"), make_user("stranger")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    base = {
        "kind": "activity",
        "scope": "group",
        "title": "Dinner",
        "starts_at": "2026-04-09T18:30:00",
        "start_timezone": "Asia/Tokyo",
    }

    # An offset with no zone to read it in can't be stored faithfully.
    offset_no_zone = {**base, "starts_at": "2026-04-09T18:30:00+09:00"}
    del offset_no_zone["start_timezone"]
    assert client.post(url, json=offset_no_zone, headers=sun).status_code == 422

    ends_first = {**base, "ends_at": "2026-04-09T17:00:00"}
    assert client.post(url, json=ends_first, headers=sun).status_code == 422

    missing_place = {**base, "place_id": str(uuid.uuid4())}
    assert client.post(url, json=missing_place, headers=sun).status_code == 422

    bad_timezone = {**base, "start_timezone": "Asia/Tokio"}
    assert client.post(url, json=bad_timezone, headers=sun).status_code == 422

    end_zone_without_end = {**base, "end_timezone": "Asia/Tokyo"}
    assert client.post(url, json=end_zone_without_end, headers=sun).status_code == 422
    end_zone_on_floating = {
        **base, "ends_at": "2026-04-09T20:00:00", "end_timezone": "Asia/Tokyo"
    }
    del end_zone_on_floating["start_timezone"]
    assert client.post(url, json=end_zone_on_floating, headers=sun).status_code == 422

    # Group items carry a requirement, personal items a visibility; never the other.
    group_with_visibility = {**base, "visibility": "shared"}
    assert client.post(url, json=group_with_visibility, headers=sun).status_code == 422
    personal_with_requirement = {**base, "scope": "personal", "requirement": "mandatory"}
    assert client.post(url, json=personal_with_requirement, headers=sun).status_code == 422

    assert client.post(url, json=base, headers=stranger).status_code == 404

    item = client.post(url, json=base, headers=sun).json()
    item_url = f"{url}/{item['id']}"
    moved_before_start = {"ends_at": "2026-04-09T10:00:00"}
    assert client.patch(item_url, json=moved_before_start, headers=sun).status_code == 422
    # The old single-zone field is gone, not silently ignored.
    assert client.patch(item_url, json={"timezone": "Asia/Tokyo"}, headers=sun).status_code == 422

    # The scope-bound field can't be nulled or swapped for the other scope's.
    assert client.patch(item_url, json={"scope": None}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"requirement": None}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"visibility": "open"}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"colour": "red"}, headers=sun).status_code == 422
    changed = client.patch(item_url, json={"requirement": "mandatory"}, headers=sun)
    assert changed.status_code == 200
    assert changed.json()["requirement"] == "mandatory"


def test_items_without_a_zone_float(client, make_user, make_trip):
    """'Dinner at my place at 7 on the 17th' needs no zone: it's kept as
    written, with no instant, and the client reads it in whatever zone fits."""
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    created = client.post(
        url,
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Dinner at my place",
            "starts_at": "2026-04-17T19:00:00",
            "ends_at": "2026-04-17T22:00:00",
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["starts_at"] == "2026-04-17T19:00:00"
    assert item["start_timezone"] is None
    assert item["starts_at_utc"] is None
    assert item["ends_at"] == "2026-04-17T22:00:00"
    assert item["end_timezone"] is None
    assert item["ends_at_utc"] is None
    item_url = f"{url}/{item['id']}"

    # Floating ends are ordered as wall times.
    assert client.patch(item_url, json={"ends_at": "2026-04-17T18:00:00"}, headers=sun).status_code == 422

    # Giving it a zone pins it; the end takes the same zone.
    pinned = client.patch(item_url, json={"start_timezone": "Asia/Tokyo"}, headers=sun)
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["starts_at"] == "2026-04-17T19:00:00"
    assert pinned.json()["starts_at_utc"] == "2026-04-17T10:00:00Z"
    assert pinned.json()["end_timezone"] == "Asia/Tokyo"
    assert pinned.json()["ends_at_utc"] == "2026-04-17T13:00:00Z"

    # And it can be set adrift again.
    floating = client.patch(item_url, json={"start_timezone": None}, headers=sun)
    assert floating.status_code == 200, floating.text
    assert floating.json()["start_timezone"] is None
    assert floating.json()["starts_at_utc"] is None
    assert floating.json()["end_timezone"] is None
    assert floating.json()["ends_at_utc"] is None

    # A floating item that gets a located place takes the place's zone.
    hnd = _place(client, sun, name="HND", timezone="Asia/Tokyo")
    located = client.patch(item_url, json={"place_id": hnd["id"]}, headers=sun)
    assert located.json()["start_timezone"] == "Asia/Tokyo"


def test_aware_times_are_converted_into_the_resolved_zone(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    created = client.post(
        f"/trips/{trip['id']}/items",
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Call from the office",
            "starts_at": "2026-04-14T09:00:00+09:00",
            "start_timezone": "America/Los_Angeles",
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    assert created.json()["starts_at"] == "2026-04-13T17:00:00"
    assert created.json()["start_timezone"] == "America/Los_Angeles"
    assert created.json()["starts_at_utc"] == "2026-04-14T00:00:00Z"


def test_patching_only_the_timezone_keeps_the_wall_time(client, make_user, make_trip):
    """The 'right time, wrong zone' fix: 09:00 stays 09:00, the instant moves."""
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    item = client.post(
        url,
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Standup",
            "starts_at": "2026-04-14T09:00:00",
            "start_timezone": "Asia/Tokyo",
        },
        headers=sun,
    ).json()
    assert item["starts_at_utc"] == "2026-04-14T00:00:00Z"

    moved = client.patch(
        f"{url}/{item['id']}", json={"start_timezone": "Europe/London"}, headers=sun
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["starts_at"] == "2026-04-14T09:00:00"
    assert moved.json()["start_timezone"] == "Europe/London"
    assert moved.json()["starts_at_utc"] == "2026-04-14T08:00:00Z"

    # A naive time alone is read in the stored zone; an aware one is converted into it.
    later = client.patch(f"{url}/{item['id']}", json={"starts_at": "2026-04-14T10:00:00"}, headers=sun)
    assert later.json()["starts_at_utc"] == "2026-04-14T09:00:00Z"
    aware = client.patch(
        f"{url}/{item['id']}", json={"starts_at": "2026-04-14T12:00:00Z"}, headers=sun
    )
    assert aware.json()["starts_at"] == "2026-04-14T13:00:00"
    assert aware.json()["start_timezone"] == "Europe/London"


def test_clearing_ends_at_clears_its_timezone(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    item = client.post(
        url,
        json={
            "kind": "lodging",
            "scope": "group",
            "title": "Hotel",
            "starts_at": "2026-04-09T15:00:00",
            "start_timezone": "Asia/Tokyo",
            "ends_at": "2026-04-11T11:00:00",
            "end_timezone": "Asia/Tokyo",
        },
        headers=sun,
    ).json()
    item_url = f"{url}/{item['id']}"
    assert item["ends_at_utc"] == "2026-04-11T02:00:00Z"

    # The end zone can't be sent without an end time; nulling it just re-resolves.
    cleared = client.patch(item_url, json={"ends_at": None}, headers=sun)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["ends_at"] is None
    assert cleared.json()["end_timezone"] is None
    assert cleared.json()["ends_at_utc"] is None
    assert client.patch(item_url, json={"end_timezone": "Asia/Tokyo"}, headers=sun).status_code == 422

    # Setting an end again with no zone falls back to the start's.
    restored = client.patch(item_url, json={"ends_at": "2026-04-12T11:00:00"}, headers=sun)
    assert restored.status_code == 200, restored.text
    assert restored.json()["end_timezone"] == "Asia/Tokyo"
    assert restored.json()["ends_at_utc"] == "2026-04-12T02:00:00Z"


def test_cross_zone_transit_may_arrive_at_an_earlier_wall_time(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    flight = {
        "kind": "transit",
        "scope": "personal",
        "title": "Fly home",
        "starts_at": "2026-04-14T17:00:00",
        "start_timezone": "Asia/Tokyo",
        "ends_at": "2026-04-14T10:00:00",
        "end_timezone": "America/Los_Angeles",
    }
    # 17:00 JST is 08:00Z; 10:00 PDT the same day is 17:00Z. Later, despite the clock.
    created = client.post(url, json=flight, headers=sun)
    assert created.status_code == 201, created.text
    assert created.json()["starts_at_utc"] == "2026-04-14T08:00:00Z"
    assert created.json()["ends_at_utc"] == "2026-04-14T17:00:00Z"

    # Landing before take-off as instants is still an error.
    backwards = {**flight, "ends_at": "2026-04-13T10:00:00"}
    assert client.post(url, json=backwards, headers=sun).status_code == 422


def test_sync_instants():
    item = TripItem(starts_at=datetime(2026, 4, 14, 9), ends_at=datetime(2026, 4, 14, 10))
    item.sync_instants()
    assert item.starts_at_utc is None and item.ends_at_utc is None  # floating

    item.start_timezone, item.ends_at = "Asia/Tokyo", None
    item.sync_instants()
    assert item.starts_at_utc == datetime(2026, 4, 14, 0, tzinfo=UTC)
    assert item.ends_at_utc is None

    item.ends_at, item.end_timezone = datetime(2026, 4, 14, 10), "America/Los_Angeles"
    item.sync_instants()
    assert item.ends_at_utc == datetime(2026, 4, 14, 17, tzinfo=UTC)

    item.starts_at = datetime(2026, 4, 14, 9, tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError):
        item.sync_instants()


def test_items_are_scoped_to_their_trip(client, make_user, make_trip):
    sun = make_user("sun")
    japan, other = make_trip(sun), make_trip(sun, title="Other")
    item = client.post(
        f"/trips/{japan['id']}/items",
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Dinner",
            "starts_at": "2026-04-09T18:30:00",
            "start_timezone": "Asia/Tokyo",
        },
        headers=sun,
    ).json()

    assert client.get(f"/trips/{other['id']}/items/{item['id']}", headers=sun).status_code == 404
    assert client.get(f"/trips/{other['id']}/items", headers=sun).json() == []


def test_itinerary_groups_items_into_local_days(client, make_user, make_trip):
    """Day 1 of the design: land, check in, Sean arrives, dinner."""
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    client.post(f"{url}/members", json={"user_id": sean.user_id}, headers=sun)

    def add(headers=sun, scope="group", start_timezone="Asia/Tokyo", **item):
        payload = {"scope": scope, "start_timezone": start_timezone, **item}
        response = client.post(f"{url}/items", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    # Inserted out of order on purpose.
    add(kind="activity", title="Omoide Yokocho dinner", starts_at="2026-04-09T18:30:00")
    # Only the landing time is known: departure and arrival are the same instant.
    add(
        kind="transit",
        title="Land at HND",
        starts_at="2026-04-09T11:00:00",
        ends_at="2026-04-09T11:00:00",
    )
    add(
        kind="lodging",
        title="Hotel Kanade Shinjuku",
        starts_at="2026-04-09T12:00:00",
        ends_at="2026-04-11T11:00:00",
    )
    # Sean posts his own flight; it's shared so the group sees when he lands.
    add(
        headers=sean,
        scope="personal",
        kind="transit",
        title="Sean Salim arrives",
        starts_at="2026-04-09T15:00:00",
    )
    # An instant is fine too: 15:30Z is 00:30 on the 10th in Tokyo.
    golden_gai = add(kind="activity", title="Golden Gai", starts_at="2026-04-09T15:30:00Z")
    assert golden_gai["starts_at"] == "2026-04-10T00:30:00"
    # A floating time sits on the date it names.
    nap = add(kind="activity", title="Jet-lag nap", starts_at="2026-04-10T14:30:00", start_timezone=None)
    assert nap["start_timezone"] is None

    response = client.get(f"{url}/itinerary", headers=sun)
    assert response.status_code == 200
    days = response.json()["days"]

    assert [day["date"] for day in days] == [f"2026-04-{d:02d}" for d in range(9, 16)]
    assert [day["day_number"] for day in days] == [1, 2, 3, 4, 5, 6, 7]

    assert [item["title"] for item in days[0]["items"]] == [
        "Land at HND",
        "Hotel Kanade Shinjuku",
        "Sean Salim arrives",
        "Omoide Yokocho dinner",
    ]
    arrival = days[0]["items"][2]
    assert arrival["created_by"]["email"] == "sean@example.com"
    assert arrival["visibility"] == "shared"
    assert [(p["user"]["email"], p["status"]) for p in arrival["participants"]] == [
        ("sean@example.com", "going")
    ]
    assert [item["title"] for item in days[1]["items"]] == ["Golden Gai", "Jet-lag nap"]

    phases = [[stay["phase"] for stay in day["lodging"]] for day in days]
    assert phases == [["check_in"], ["staying"], ["check_out"], [], [], [], []]
    assert days[2]["items"] == []
    assert days[0]["items"][0]["start_timezone"] == "Asia/Tokyo"


def test_itinerary_days_are_ordered_by_wall_time_regardless_of_zone(client, make_user, make_trip):
    """A day reads like a diary page: by the clock, whatever zone each entry is in."""
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"

    def add(title, starts_at, start_timezone=None, **item):
        payload = {
            "kind": "activity",
            "scope": "group",
            "title": title,
            "starts_at": starts_at,
            "start_timezone": start_timezone,
            **item,
        }
        assert client.post(f"{url}/items", json=payload, headers=sun).status_code == 201

    # As instants these run LA 10:00 (17:00Z) last, Tokyo 09:00 (00:00Z) first.
    add("Call from LA", "2026-04-10T10:00:00", "America/Los_Angeles")
    add("Tokyo breakfast", "2026-04-10T09:00:00", "Asia/Tokyo")
    add("Packing", "2026-04-10T08:00:00")
    add("Lunch", "2026-04-10T12:30:00")
    # A flight is listed by its departure clock on one day and its arrival clock on the next.
    add(
        "Overnight to Tokyo",
        "2026-04-10T13:00:00",
        "America/Los_Angeles",
        kind="transit",
        ends_at="2026-04-11T16:00:00",
        end_timezone="Asia/Tokyo",
    )
    add("Check in", "2026-04-11T15:00:00", "Asia/Tokyo")

    days = client.get(f"{url}/itinerary", headers=sun).json()["days"]
    assert [i["title"] for i in days[1]["items"]] == [
        "Packing",
        "Tokyo breakfast",
        "Call from LA",
        "Lunch",
        "Overnight to Tokyo",
    ]
    assert [i["title"] for i in days[2]["items"]] == ["Check in", "Overnight to Tokyo"]


def test_itinerary_uses_place_timezones_and_keeps_out_of_range_items(
    client, make_user, make_trip
):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    lax = _place(client, sun, name="LAX", timezone="America/Los_Angeles")
    hnd = _place(client, sun, name="HND", timezone="Asia/Tokyo")

    # Sent as instants with no zones: each end takes its airport's. Leaves LA
    # on the afternoon of the 8th; lands at 22:00 UTC on the 9th, which is
    # already 07:00 on the 10th in Tokyo.
    flight = client.post(
        f"{url}/items",
        json={
            "kind": "transit",
            "scope": "personal",
            "title": "Flight to Tokyo",
            "starts_at": "2026-04-08T23:00:00Z",
            "ends_at": "2026-04-09T22:00:00Z",
            "place_id": lax["id"],
            "end_place_id": hnd["id"],
        },
        headers=sun,
    ).json()
    assert flight["starts_at"] == "2026-04-08T16:00:00"
    assert flight["start_timezone"] == "America/Los_Angeles"
    assert flight["ends_at"] == "2026-04-10T07:00:00"
    assert flight["end_timezone"] == "Asia/Tokyo"
    assert flight["ends_at_utc"] == "2026-04-09T22:00:00Z"
    client.post(
        f"{url}/items",
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Breakfast",
            "starts_at": "2026-04-10T06:00:00",
            "start_timezone": "Asia/Tokyo",
        },
        headers=sun,
    )

    days = client.get(f"{url}/itinerary", headers=sun).json()["days"]
    assert days[0]["date"] == "2026-04-08"
    assert days[0]["day_number"] == 0
    assert [item["title"] for item in days[0]["items"]] == ["Flight to Tokyo"]
    # The same item shows again on its arrival day, in the destination's zone,
    # sorted by when it lands rather than when it took off.
    assert days[1]["items"] == []
    assert [item["title"] for item in days[2]["items"]] == ["Breakfast", "Flight to Tokyo"]
    assert days[2]["items"][1]["id"] == flight["id"]
    assert len(days) == 8
