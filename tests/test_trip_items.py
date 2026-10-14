import uuid


def _place(client, headers, **fields):
    response = client.post("/places", json=fields, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_item_round_trip_with_places(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}/items"
    kyoto = _place(client, sun, name="Kyoto Station")
    osaka = _place(client, sun, name="Shin-Osaka Station")

    created = client.post(
        url,
        json={
            "kind": "transit",
            "scope": "group",
            "title": "Train to Osaka",
            "tags": ["Transit", "JR"],
            "starts_at": "2026-04-14T09:00:00+09:00",
            "ends_at": "2026-04-14T09:30:00+09:00",
            "place_id": kyoto["id"],
            "end_place_id": osaka["id"],
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["place"]["name"] == "Kyoto Station"
    assert item["end_place"]["name"] == "Shin-Osaka Station"
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
        "starts_at": "2026-04-09T18:30:00+09:00",
    }

    naive_time = {**base, "starts_at": "2026-04-09T18:30:00"}
    assert client.post(url, json=naive_time, headers=sun).status_code == 422

    ends_first = {**base, "ends_at": "2026-04-09T17:00:00+09:00"}
    assert client.post(url, json=ends_first, headers=sun).status_code == 422

    missing_place = {**base, "place_id": str(uuid.uuid4())}
    assert client.post(url, json=missing_place, headers=sun).status_code == 422

    bad_timezone = {**base, "timezone": "Asia/Tokio"}
    assert client.post(url, json=bad_timezone, headers=sun).status_code == 422

    # Group items carry a requirement, personal items a visibility; never the other.
    group_with_visibility = {**base, "visibility": "shared"}
    assert client.post(url, json=group_with_visibility, headers=sun).status_code == 422
    personal_with_requirement = {**base, "scope": "personal", "requirement": "mandatory"}
    assert client.post(url, json=personal_with_requirement, headers=sun).status_code == 422

    assert client.post(url, json=base, headers=stranger).status_code == 404

    item = client.post(url, json=base, headers=sun).json()
    moved_before_start = {"ends_at": "2026-04-09T10:00:00+09:00"}
    response = client.patch(f"{url}/{item['id']}", json=moved_before_start, headers=sun)
    assert response.status_code == 422

    # The scope-bound field can't be nulled or swapped for the other scope's.
    item_url = f"{url}/{item['id']}"
    assert client.patch(item_url, json={"scope": None}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"requirement": None}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"visibility": "open"}, headers=sun).status_code == 422
    assert client.patch(item_url, json={"colour": "red"}, headers=sun).status_code == 422
    changed = client.patch(item_url, json={"requirement": "mandatory"}, headers=sun)
    assert changed.status_code == 200
    assert changed.json()["requirement"] == "mandatory"


def test_items_are_scoped_to_their_trip(client, make_user, make_trip):
    sun = make_user("sun")
    japan, other = make_trip(sun), make_trip(sun, title="Other")
    item = client.post(
        f"/trips/{japan['id']}/items",
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Dinner",
            "starts_at": "2026-04-09T18:30:00+09:00",
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

    def add(headers=sun, scope="group", timezone="Asia/Tokyo", **item):
        payload = {"scope": scope, "timezone": timezone, **item}
        response = client.post(f"{url}/items", json=payload, headers=headers)
        assert response.status_code == 201, response.text

    # Inserted out of order on purpose.
    add(kind="activity", title="Omoide Yokocho dinner", starts_at="2026-04-09T18:30:00+09:00")
    # Only the landing time is known: departure and arrival are the same instant.
    add(
        kind="transit",
        title="Land at HND",
        starts_at="2026-04-09T11:00:00+09:00",
        ends_at="2026-04-09T11:00:00+09:00",
    )
    add(
        kind="lodging",
        title="Hotel Kanade Shinjuku",
        starts_at="2026-04-09T12:00:00+09:00",
        ends_at="2026-04-11T11:00:00+09:00",
    )
    # Sean posts his own flight; it's shared so the group sees when he lands.
    add(
        headers=sean,
        scope="personal",
        kind="transit",
        title="Sean Salim arrives",
        starts_at="2026-04-09T15:00:00+09:00",
    )
    # 00:30 on the 10th in Tokyo is still the 9th in UTC.
    add(kind="activity", title="Golden Gai", starts_at="2026-04-09T15:30:00Z")
    # Without a timezone or a located place, days are bucketed in UTC.
    add(kind="activity", title="Jet-lag nap", starts_at="2026-04-10T23:30:00+09:00", timezone=None)

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
    assert days[0]["items"][0]["timezone"] == "Asia/Tokyo"


def test_itinerary_uses_place_timezone_and_keeps_out_of_range_items(
    client, make_user, make_trip
):
    sun = make_user("sun")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    lax = _place(client, sun, name="LAX", timezone="America/Los_Angeles")
    hnd = _place(client, sun, name="HND", timezone="Asia/Tokyo")

    # Leaves LA on the afternoon of the 8th. Lands at 22:00 UTC on the 9th,
    # which is still the 9th in LA but already 07:00 on the 10th in Tokyo.
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
    client.post(
        f"{url}/items",
        json={
            "kind": "activity",
            "scope": "group",
            "title": "Breakfast",
            "starts_at": "2026-04-10T06:00:00+09:00",
            "timezone": "Asia/Tokyo",
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
    assert days[2]["items"][1]["ends_at"] == "2026-04-09T22:00:00Z"
    assert len(days) == 8
