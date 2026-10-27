"""People on a trip by email, with or without an account: setting the roster
through the trip itself, and recording their answers on items."""


def _roles(trip: dict) -> dict[str, str]:
    return {m["email"]: m["role"] for m in trip["members"]}


def _rsvps(item: dict) -> dict[str, str]:
    return {p["member"]["email"]: p["status"] for p in item["participants"]}


def _member_ids(trip: dict) -> dict[str, str]:
    return {m["email"]: m["id"] for m in trip["members"]}


def test_creating_a_trip_with_members(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(
        sun,
        members=[
            {"email": "Grandma@Example.com"},
            {"email": sean.email, "role": "organizer"},
            {"email": sun.email, "role": "organizer"},  # the owner, listed or not
        ],
    )

    assert _roles(trip) == {
        "sun@example.com": "organizer",
        "grandma@example.com": "member",
        "sean@example.com": "organizer",
    }
    by_email = {m["email"]: m for m in trip["members"]}
    assert by_email["grandma@example.com"]["user"] is None
    assert by_email["sean@example.com"]["user"]["id"] == sean.user_id

    # An account on the roster can open the trip right away.
    assert client.get(f"/trips/{trip['id']}", headers=sean).status_code == 200

    dupes = [{"email": "a@example.com"}, {"email": "A@example.com"}]
    response = client.post(
        "/trips",
        json={"title": "T", "start_date": "2026-04-09", "end_date": "2026-04-10", "members": dupes},
        headers=sun,
    )
    assert response.status_code == 422


def test_registering_links_the_account(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun, members=[{"email": "sean@example.com"}])
    url = f"/trips/{trip['id']}"

    sean = make_user("sean")
    roster = client.get(f"{url}/members", headers=sun).json()
    assert {m["email"]: (m["user"] or {}).get("id") for m in roster}["sean@example.com"] == (
        sean.user_id
    )
    assert client.get(url, headers=sean).status_code == 200
    assert [t["id"] for t in client.get("/trips", headers=sean).json()] == [trip["id"]]


def test_patching_the_roster(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, members=[{"email": "grandma@example.com"}, {"email": sean.email}])
    url = f"/trips/{trip['id']}"
    ids = _member_ids(trip)
    coffee = {
        "kind": "activity",
        "title": "Coffee",
        "starts_at": "2026-04-10T09:00:00",
    }
    assert client.post(f"{url}/items", json=coffee, headers=sean).status_code == 201

    # Leaving members out of a PATCH leaves the roster alone.
    assert _roles(client.patch(url, json={"title": "Japan!"}, headers=sun).json()) == _roles(trip)

    # A list replaces the roster: roles change in place (same rows), new
    # emails are added, and anyone missing is removed. The owner stays even
    # though they're not listed.
    patched = client.patch(
        url,
        json={
            "members": [
                {"email": "grandma@example.com", "role": "organizer"},
                {"email": "amy@example.com"},
            ]
        },
        headers=sun,
    )
    assert patched.status_code == 200, patched.text
    assert _roles(patched.json()) == {
        "sun@example.com": "organizer",
        "grandma@example.com": "organizer",
        "amy@example.com": "member",
    }
    assert _member_ids(patched.json())["grandma@example.com"] == ids["grandma@example.com"]

    # Sean is off the trip and off his item, which stays on the trip.
    assert client.get(url, headers=sean).status_code == 404
    items = client.get(f"{url}/items", headers=sun).json()
    assert [(i["title"], i["participants"]) for i in items] == [("Coffee", [])]

    # The owner can't be demoted, and only organizers edit the roster.
    owner_demoted = client.patch(
        url, json={"members": [{"email": sun.email, "role": "member"}]}, headers=sun
    )
    assert owner_demoted.status_code == 409
    amy = make_user("amy")  # links to the row she was just added with
    assert client.patch(url, json={"members": []}, headers=amy).status_code == 403


def test_items_can_be_for_people_without_accounts(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, members=[{"email": "grandma@example.com"}, {"email": sean.email}])
    url = f"/trips/{trip['id']}"
    ids = _member_ids(trip)

    created = client.post(
        f"{url}/items",
        json={
            "kind": "activity",
            "title": "Rehearsal dinner",
            "starts_at": "2026-04-10T19:00:00",
            "participants": [{"member_id": ids["grandma@example.com"], "status": "going"}],
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    dinner = created.json()
    assert _rsvps(dinner) == {"grandma@example.com": "going"}
    assert dinner["participants"][0]["member"]["user"] is None
    item_url = f"{url}/items/{dinner['id']}"

    # Sean answers for himself, which adds him. A PATCH replaces the set:
    # listed members keep their answer unless a new one is sent.
    client.put(f"{item_url}/rsvp", json={"status": "maybe"}, headers=sean)
    patched = client.patch(
        item_url,
        json={
            "participants": [
                {"member_id": ids["grandma@example.com"], "status": "declined"},
                {"member_id": ids["sean@example.com"]},
            ]
        },
        headers=sun,
    )
    assert patched.status_code == 200, patched.text
    assert _rsvps(patched.json()) == {
        "grandma@example.com": "declined",
        "sean@example.com": "maybe",
    }
    cleared = client.patch(item_url, json={"participants": []}, headers=sun).json()
    assert _rsvps(cleared) == {}

    # Any member can edit who an item is for, for now.
    by_sean = client.patch(
        item_url, json={"participants": [{"member_id": ids["grandma@example.com"]}]}, headers=sean
    )
    assert by_sean.status_code == 200
    assert _rsvps(by_sean.json()) == {"grandma@example.com": None}

    # Members must be on this trip, and appear once.
    other_trip = make_trip(sun, title="Italy", members=[{"email": "amy@example.com"}])
    stranger = _member_ids(other_trip)["amy@example.com"]
    bad = client.patch(
        item_url, json={"participants": [{"member_id": stranger, "status": "going"}]}, headers=sun
    )
    assert bad.status_code == 422
    twice = [{"member_id": ids["sean@example.com"], "status": "going"}] * 2
    assert client.patch(item_url, json={"participants": twice}, headers=sun).status_code == 422

    # Removing someone takes their answers with them.
    grandma_going = [{"member_id": ids["grandma@example.com"], "status": "going"}]
    client.patch(item_url, json={"participants": grandma_going}, headers=sun)
    removed = client.delete(f"{url}/members/{ids['grandma@example.com']}", headers=sun)
    assert removed.status_code == 204
    assert _rsvps(client.get(item_url, headers=sun).json()) == {}


def test_answers_carry_over_when_someone_signs_up(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun, members=[{"email": "sean@example.com"}])
    url = f"/trips/{trip['id']}"
    ids = _member_ids(trip)
    dinner = client.post(
        f"{url}/items",
        json={
            "kind": "activity",
            "title": "Dinner",
            "starts_at": "2026-04-10T19:00:00",
            "participants": [{"member_id": ids["sean@example.com"], "status": "maybe"}],
        },
        headers=sun,
    ).json()

    sean = make_user("sean")
    item_url = f"{url}/items/{dinner['id']}"
    assert _rsvps(client.get(item_url, headers=sean).json()) == {"sean@example.com": "maybe"}
    changed = client.put(f"{item_url}/rsvp", json={"status": "going"}, headers=sean)
    assert _rsvps(changed.json()) == {"sean@example.com": "going"}


def test_member_ids_are_scoped_to_their_trip(client, make_user, make_trip):
    sun = make_user("sun")
    japan = make_trip(sun, members=[{"email": "grandma@example.com"}])
    italy = make_trip(sun, title="Italy")
    grandma = _member_ids(japan)["grandma@example.com"]
    assert client.delete(f"/trips/{italy['id']}/members/{grandma}", headers=sun).status_code == 404
