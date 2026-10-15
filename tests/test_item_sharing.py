"""Group vs personal items, who can see and change them, and RSVPs."""

from datetime import datetime, timedelta

import pytest

FIRST_AT = datetime(2026, 4, 10, 9, 0)  # floating wall time; the wedding needs no zone


def _rsvps(item: dict) -> dict[str, str]:
    return {p["user"]["email"]: p["status"] for p in item["participants"]}


@pytest.fixture
def wedding(client, make_user, make_trip):
    """Sun organizes; Sean and Amy are members."""
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(sun, title="Wedding")
    url = f"/trips/{trip['id']}"
    for guest in (sean, amy):
        response = client.post(f"{url}/members", json={"user_id": guest.user_id}, headers=sun)
        assert response.status_code == 201, response.text

    clock = iter(range(1, 100))

    def add(headers, **fields):
        """Each item starts an hour after the previous one, so lists come back in
        creation order."""
        starts_at = (FIRST_AT + timedelta(hours=next(clock))).isoformat()
        payload = {"kind": "activity", "starts_at": starts_at, **fields}
        response = client.post(f"{url}/items", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return {"url": url, "sun": sun, "sean": sean, "amy": amy, "add": add}


def _titles(client, url, headers, **params):
    response = client.get(f"{url}/items", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()]


def test_private_items_are_only_visible_to_their_creator(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    add(sun, scope="group", title="Ceremony", requirement="mandatory")
    add(sean, scope="personal", title="Call home", visibility="private")
    shared = add(sean, scope="personal", title="Dinner with cousins")  # defaults to shared
    secret = add(sean, scope="personal", title="Ring shopping", visibility="private")

    assert _titles(client, url, sean) == [
        "Ceremony",
        "Call home",
        "Dinner with cousins",
        "Ring shopping",
    ]
    assert _titles(client, url, amy) == ["Ceremony", "Dinner with cousins"]
    assert _titles(client, url, sun) == ["Ceremony", "Dinner with cousins"]
    assert _titles(client, url, amy, scope="personal") == ["Dinner with cousins"]

    # Reading a private item directly 404s, like a trip you're not on.
    assert client.get(f"{url}/items/{secret['id']}", headers=amy).status_code == 404
    assert client.get(f"{url}/items/{secret['id']}", headers=sun).status_code == 404
    assert client.get(f"{url}/items/{secret['id']}", headers=sean).status_code == 200

    # Shared items say who added them.
    visible = client.get(f"{url}/items/{shared['id']}", headers=amy).json()
    assert visible["created_by"]["email"] == "sean@example.com"
    assert _rsvps(visible) == {"sean@example.com": "going"}

    # The itinerary applies the same filter.
    days = client.get(f"{url}/itinerary", headers=amy).json()["days"]
    assert [i["title"] for day in days for i in day["items"]] == ["Ceremony", "Dinner with cousins"]
    days = client.get(f"{url}/itinerary", headers=sean).json()["days"]
    assert len([i for day in days for i in day["items"]]) == 4


def test_only_the_creator_changes_a_personal_item(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    item = add(sean, scope="personal", title="Dinner with cousins")
    item_url = f"{url}/items/{item['id']}"

    # Neither another member nor the organizer can touch it.
    assert client.patch(item_url, json={"title": "Hijacked"}, headers=amy).status_code == 403
    assert client.patch(item_url, json={"title": "Hijacked"}, headers=sun).status_code == 403
    assert client.delete(item_url, headers=sun).status_code == 403

    changed = client.patch(item_url, json={"visibility": "open"}, headers=sean)
    assert changed.status_code == 200
    assert changed.json()["visibility"] == "open"
    assert client.delete(item_url, headers=sean).status_code == 204


def test_only_organizers_change_group_items(client, wedding):
    url, sun, sean, add = (wedding[k] for k in ("url", "sun", "sean", "add"))
    item = add(sun, scope="group", title="Ceremony", requirement="mandatory")
    item_url = f"{url}/items/{item['id']}"

    assert client.patch(item_url, json={"title": "Moved"}, headers=sean).status_code == 403
    assert client.delete(item_url, headers=sean).status_code == 403
    assert client.patch(item_url, json={"title": "Moved"}, headers=sun).status_code == 200
    assert client.delete(item_url, headers=sun).status_code == 204


def test_rsvp_on_group_items(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    item = add(sun, scope="group", title="Rehearsal dinner", requirement="recommended")
    rsvp_url = f"{url}/items/{item['id']}/rsvp"

    assert _rsvps(item) == {}

    going = client.put(rsvp_url, json={"status": "going"}, headers=sean)
    assert going.status_code == 200, going.text
    assert _rsvps(going.json()) == {"sean@example.com": "going"}

    # Changing your mind upserts; the organizer sees every answer.
    client.put(rsvp_url, json={"status": "declined"}, headers=sean)
    client.put(rsvp_url, json={"status": "maybe"}, headers=amy)
    seen_by_organizer = client.get(f"{url}/items/{item['id']}", headers=sun).json()
    assert _rsvps(seen_by_organizer) == {"sean@example.com": "declined", "amy@example.com": "maybe"}

    # Withdrawing clears the row; doing it again is a 404.
    assert client.delete(rsvp_url, headers=sean).status_code == 204
    assert client.delete(rsvp_url, headers=sean).status_code == 404
    assert _rsvps(client.get(f"{url}/items/{item['id']}", headers=sun).json()) == {
        "amy@example.com": "maybe"
    }

    assert client.put(rsvp_url, json={"status": "yes"}, headers=amy).status_code == 422


def test_joining_personal_items(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    open_item = add(sean, scope="personal", title="Karaoke", visibility="open")
    shared_item = add(sean, scope="personal", title="Family brunch", visibility="shared")
    open_rsvp = f"{url}/items/{open_item['id']}/rsvp"
    shared_rsvp = f"{url}/items/{shared_item['id']}/rsvp"

    joined = client.put(open_rsvp, json={"status": "going"}, headers=amy)
    assert joined.status_code == 200, joined.text
    assert _rsvps(joined.json()) == {"sean@example.com": "going", "amy@example.com": "going"}
    # Organizers join like anyone else.
    assert client.put(open_rsvp, json={"status": "maybe"}, headers=sun).status_code == 200

    # Shared-but-not-open is look, don't touch.
    assert client.put(shared_rsvp, json={"status": "going"}, headers=amy).status_code == 403
    assert client.put(shared_rsvp, json={"status": "going"}, headers=sun).status_code == 403

    # Leaving works for joiners; the creator has to delete the item instead.
    assert client.delete(open_rsvp, headers=amy).status_code == 204
    assert client.delete(open_rsvp, headers=sean).status_code == 409
    assert _rsvps(client.get(f"{url}/items/{open_item['id']}", headers=sean).json()) == {
        "sean@example.com": "going",
        "sun@example.com": "maybe",
    }

    # Someone who joined keeps seeing the item even after it goes private.
    client.put(open_rsvp, json={"status": "going"}, headers=amy)
    client.patch(f"{url}/items/{open_item['id']}", json={"visibility": "private"}, headers=sean)
    assert client.get(f"{url}/items/{open_item['id']}", headers=amy).status_code == 200


def test_removing_a_member_takes_their_personal_items_and_rsvps(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))
    ceremony = add(sun, scope="group", title="Ceremony", requirement="mandatory")
    client.put(f"{url}/items/{ceremony['id']}/rsvp", json={"status": "going"}, headers=sean)
    add(sean, scope="personal", title="Dinner with cousins")
    karaoke = add(amy, scope="personal", title="Karaoke", visibility="open")
    client.put(f"{url}/items/{karaoke['id']}/rsvp", json={"status": "going"}, headers=sean)

    # Promote Sean so he can post a group item, which should outlive him.
    client.delete(f"{url}/members/{sean.user_id}", headers=sun)
    client.post(
        f"{url}/members", json={"user_id": sean.user_id, "role": "organizer"}, headers=sun
    )
    add(sean, scope="group", title="After party")

    assert client.delete(f"{url}/members/{sean.user_id}", headers=sun).status_code == 204

    assert _titles(client, url, sun) == ["Ceremony", "Karaoke", "After party"]
    assert _rsvps(client.get(f"{url}/items/{ceremony['id']}", headers=sun).json()) == {}
    assert _rsvps(client.get(f"{url}/items/{karaoke['id']}", headers=sun).json()) == {
        "amy@example.com": "going"
    }
    after_party = client.get(f"{url}/items", headers=sun, params={"scope": "group"}).json()[-1]
    assert after_party["created_by"]["email"] == "sean@example.com"


def test_personal_items_can_sit_outside_the_trip_dates(client, wedding):
    """A member's own travel before the trip shows up on extra leading days."""
    url, sean, add = wedding["url"], wedding["sean"], wedding["add"]
    add(
        sean,
        scope="personal",
        kind="transit",
        title="Fly in early",
        starts_at="2026-04-07T09:00:00",
    )

    days = client.get(f"{url}/itinerary", headers=sean).json()["days"]
    assert days[0]["date"] == "2026-04-07"
    assert days[0]["day_number"] == -1
    assert [i["title"] for i in days[0]["items"]] == ["Fly in early"]


def test_changing_scope(client, wedding):
    url, sun, sean, amy, add = (wedding[k] for k in ("url", "sun", "sean", "amy", "add"))

    # A member can't turn their own item into a group item: that needs an organizer.
    mine = add(sean, scope="personal", title="Karaoke", visibility="open")
    denied = client.patch(f"{url}/items/{mine['id']}", json={"scope": "group"}, headers=sean)
    assert denied.status_code == 403

    # An organizer can't take someone else's personal item and make it group either.
    denied = client.patch(f"{url}/items/{mine['id']}", json={"scope": "group"}, headers=sun)
    assert denied.status_code == 403

    # The organizer who created a group item can make it their personal one. RSVPs
    # become attendees: declines drop out, and the creator is always in.
    dinner = add(sun, scope="group", title="Rehearsal dinner", requirement="mandatory")
    client.put(f"{url}/items/{dinner['id']}/rsvp", json={"status": "going"}, headers=sean)
    client.put(f"{url}/items/{dinner['id']}/rsvp", json={"status": "declined"}, headers=amy)
    # Another organizer who didn't create it can't re-home it.
    client.post(f"{url}/members", json={"user_id": amy.user_id, "role": "organizer"}, headers=sun)
    denied = client.patch(f"{url}/items/{dinner['id']}", json={"scope": "personal"}, headers=amy)
    assert denied.status_code == 403

    demoted = client.patch(f"{url}/items/{dinner['id']}", json={"scope": "personal"}, headers=sun)
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["scope"] == "personal"
    assert demoted.json()["requirement"] is None
    assert demoted.json()["visibility"] == "shared"  # default when none was sent
    assert _rsvps(demoted.json()) == {"sean@example.com": "going", "sun@example.com": "going"}

    # And back to group, this time with the requirement spelled out. The stale
    # visibility is cleared without the client having to null it.
    promoted = client.patch(
        f"{url}/items/{dinner['id']}",
        json={"scope": "group", "requirement": "recommended"},
        headers=sun,
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["requirement"] == "recommended"
    assert promoted.json()["visibility"] is None
    assert _rsvps(promoted.json()) == {"sean@example.com": "going", "sun@example.com": "going"}

    # Sending the other scope's field alongside a scope change is still rejected.
    wrong = client.patch(
        f"{url}/items/{dinner['id']}",
        json={"scope": "personal", "requirement": "mandatory"},
        headers=sun,
    )
    assert wrong.status_code == 422
