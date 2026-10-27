"""Who an item is for (its participants) and what everyone else sees of it
(its visibility: private, open or closed)."""

from datetime import datetime, timedelta

import pytest

FIRST_AT = datetime(2026, 4, 10, 9, 0)  # floating wall time; the wedding needs no zone


def _titles(client, url, headers):
    response = client.get(f"{url}/items", headers=headers)
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()]


def _rsvps(item: dict) -> dict[str, str | None]:
    return {p["member"]["email"]: p["status"] for p in item["participants"]}


def _place(client, headers, name):
    stub = client.post("/places", json={"google_place_id": f"g-{name}"}, headers=headers)
    assert stub.status_code == 201, stub.text
    return client.patch(f"/places/{stub.json()['id']}", json={"name": name}, headers=headers).json()


@pytest.fixture
def wedding(client, make_user, make_trip):
    """Sun organizes; Sean and Amy are on the trip, and Grandma has no account."""
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(
        sun,
        title="Wedding",
        members=[{"email": sean.email}, {"email": amy.email}, {"email": "grandma@example.com"}],
    )
    url = f"/trips/{trip['id']}"
    ids = {m["email"].split("@")[0]: m["id"] for m in trip["members"]}
    clock = iter(range(1, 100))

    def add(headers, participants=None, **fields):
        """Each item starts an hour after the previous one, so lists come back in
        creation order. `participants` names members, e.g. ["sun", "grandma"]."""
        starts_at = (FIRST_AT + timedelta(hours=next(clock))).isoformat()
        payload = {"kind": "activity", **fields}
        if "segments" not in fields:  # a multi-leg transit takes its times from its legs
            payload["starts_at"] = starts_at
        if participants is not None:
            payload["participants"] = [{"member_id": ids[name]} for name in participants]
        response = client.post(f"{url}/items", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return {"url": url, "sun": sun, "sean": sean, "amy": amy, "ids": ids, "add": add}


def test_items_are_for_their_listed_participants(client, wedding):
    url, sun, sean, add = (wedding[k] for k in ("url", "sun", "sean", "add"))

    # Left out, it's just for whoever added it.
    mine = add(sean, title="Call home")
    assert _rsvps(mine) == {"sean@example.com": None}

    # Nobody is implied: a whole-trip item lists everyone, Grandma included.
    everyone = add(sun, participants=["sun", "sean", "amy", "grandma"], title="Ceremony")
    assert set(_rsvps(everyone)) == {
        "sun@example.com",
        "sean@example.com",
        "amy@example.com",
        "grandma@example.com",
    }

    # The creator needn't be on it.
    for_grandma = add(sun, participants=["grandma"], title="Grandma's taxi")
    assert _rsvps(for_grandma) == {"grandma@example.com": None}

    # Any member can add and edit items, for anyone, for now.
    edited = client.patch(
        f"{url}/items/{everyone['id']}", json={"title": "Ceremony (moved)"}, headers=sean
    )
    assert edited.status_code == 200
    assert client.delete(f"{url}/items/{for_grandma['id']}", headers=sean).status_code == 204


def test_private_items_are_only_for_participants(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    secret = add(sean, participants=["sean", "amy"], title="Ring shopping", visibility="private")
    # Even its creator loses sight of it if they aren't on it.
    add(sun, participants=["grandma"], title="Grandma's surprise", visibility="private")

    assert _titles(client, url, sean) == ["Ring shopping"]
    assert _titles(client, url, amy) == ["Ring shopping"]
    assert _titles(client, url, sun) == []
    assert client.get(f"{url}/items/{secret['id']}", headers=sun).status_code == 404
    assert client.patch(
        f"{url}/items/{secret['id']}", json={"title": "x"}, headers=sun
    ).status_code == 404
    itinerary = client.get(f"{url}/itinerary", headers=sun).json()
    assert all(not day["items"] for day in itinerary["days"])


def test_open_items_show_everything_to_everyone(client, wedding):
    url, sun, amy, add = (wedding[k] for k in ("url", "sun", "amy", "add"))
    add(sun, participants=["sun"], title="Spa", notes="Ask for Yuki", confirmation_number="X1")

    [spa] = client.get(f"{url}/items", headers=amy).json()
    assert spa["redacted"] is False
    assert (spa["notes"], spa["confirmation_number"]) == ("Ask for Yuki", "X1")
    assert _rsvps(spa) == {"sun@example.com": None}


def test_closed_items_show_only_what_and_when_to_non_participants(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    tokyo, seoul = _place(client, sun, "Haneda"), _place(client, sun, "Incheon")
    flight = add(
        sean,
        participants=["sean"],
        kind="transit",
        title="Flight home",
        visibility="closed",
        notes="Seat 12A",
        confirmation_number="PNR123",
        tags=["work"],
        segments=[
            {
                "starts_at": "2026-04-16T10:00:00",
                "ends_at": "2026-04-16T12:30:00",
                "place_id": tokyo["id"],
                "end_place_id": seoul["id"],
                "carrier": "KE",
                "flight_number": "KE2102",
                "transit_mode": "flight",
            },
            {
                "starts_at": "2026-04-16T15:00:00",
                "ends_at": "2026-04-16T23:00:00",
                "place_id": seoul["id"],
                "carrier": "KE",
                "flight_number": "KE17",
                "transit_mode": "flight",
            },
        ],
    )

    # Its participant sees it all.
    assert flight["redacted"] is False
    assert flight["notes"] == "Seat 12A"
    assert len(flight["segments"]) == 2

    # Everyone else sees that something is on, and when.
    [seen] = client.get(f"{url}/items", headers=amy).json()
    assert seen["redacted"] is True
    assert (seen["kind"], seen["title"], seen["visibility"]) == ("transit", "Flight home", "closed")
    assert (seen["starts_at"], seen["ends_at"]) == ("2026-04-16T10:00:00", "2026-04-16T23:00:00")
    hidden = ("notes", "confirmation_number", "transit_mode", "image_url", "place", "end_place")
    assert all(seen[field] is None for field in hidden)
    assert seen["tags"] == seen["segments"] == seen["participants"] == []
    assert seen["created_by"] is None
    assert client.get(f"{url}/items/{flight['id']}", headers=amy).json() == seen

    # The itinerary hides the same things: no layover stop, no places to refresh.
    itinerary = client.get(f"{url}/itinerary", headers=amy).json()
    entries = [e for day in itinerary["days"] for e in day["items"]]
    assert [e["phase"] for e in entries] == ["departure", "arrival"]
    assert all(e["item"]["redacted"] for e in entries)
    assert itinerary["stale_place_ids"] == []
    theirs = client.get(f"{url}/itinerary", headers=sean).json()
    phases = [e["phase"] for day in theirs["days"] for e in day["items"]]
    assert phases == ["departure", "layover", "arrival"]
    assert set(theirs["stale_place_ids"]) == {tokyo["id"], seoul["id"]}

    # Adding someone as a participant opens it up to them.
    client.patch(
        f"{url}/items/{flight['id']}",
        json={"participants": [{"member_id": wedding["ids"][n]} for n in ("sean", "amy")]},
        headers=sean,
    )
    assert client.get(f"{url}/items/{flight['id']}", headers=amy).json()["notes"] == "Seat 12A"


def test_rsvps_add_you_and_clearing_keeps_you_on(client, wedding):
    url, sun, sean, add = (wedding[k] for k in ("url", "sun", "sean", "add"))
    dinner = add(sun, participants=["sun"], title="Dinner")
    rsvp_url = f"{url}/items/{dinner['id']}/rsvp"

    answered = client.put(rsvp_url, json={"status": "going"}, headers=sean)
    assert answered.status_code == 200
    assert _rsvps(answered.json()) == {"sun@example.com": None, "sean@example.com": "going"}

    assert client.delete(rsvp_url, headers=sean).status_code == 204
    after = client.get(f"{url}/items/{dinner['id']}", headers=sean).json()
    assert _rsvps(after) == {"sun@example.com": None, "sean@example.com": None}
    assert client.delete(rsvp_url, headers=sean).status_code == 404


def test_removing_a_member_takes_them_off_items(client, wedding):
    url, sun, sean, add = (wedding[k] for k in ("url", "sun", "sean", "add"))
    add(sean, participants=["sean"], title="Sean's flight")
    ceremony = add(sun, participants=["sun", "sean"], title="Ceremony")

    removed = client.delete(f"{url}/members/{wedding['ids']['sean']}", headers=sun)
    assert removed.status_code == 204

    # The items stay; he's just off them.
    assert _titles(client, url, sun) == ["Sean's flight", "Ceremony"]
    after = client.get(f"{url}/items/{ceremony['id']}", headers=sun).json()
    assert _rsvps(after) == {"sun@example.com": None}
