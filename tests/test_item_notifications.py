"""Adding or changing a trip item, optionally announced to the members who can
see it."""

from datetime import datetime

import pytest

from app.services.item_notifications import day_and_time


def _inbox(client, headers) -> list[dict]:
    response = client.get("/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _bodies(client, headers) -> list[str]:
    # Notifications from one test share a timestamp (one transaction), so
    # don't rely on their order.
    return [n["body"] for n in _inbox(client, headers)]


@pytest.fixture
def wedding(client, make_user, make_trip):
    """Sun organizes; Sean and Amy are on the trip, and Grandma has no account."""
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(
        sun,
        title="Wedding",
        members=[{"email": sean.email}, {"email": amy.email}, {"email": "grandma@example.com"}],
    )
    url = f"/trips/{trip['id']}/items"
    ids = {m["email"].split("@")[0]: m["id"] for m in trip["members"]}

    def add(headers=sun, participants=("sun",), **fields):
        payload = {
            "kind": "activity",
            "title": "Dinner",
            "starts_at": "2026-04-10T19:00:00",
            "participants": [{"member_id": ids[name]} for name in participants],
            **fields,
        }
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    def edit(item, headers=sun, participants=None, **fields):
        if participants is not None:
            fields["participants"] = [{"member_id": ids[name]} for name in participants]
        response = client.patch(f"{url}/{item['id']}", json=fields, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    return {
        "trip": trip,
        "sun": sun,
        "sean": sean,
        "amy": amy,
        "add": add,
        "edit": edit,
    }


def test_items_are_quiet_unless_asked(client, wedding, pushes):
    item = wedding["add"]()
    wedding["edit"](item, title="Supper")
    wedding["edit"](item, title="Brunch", notify_members=False)
    for who in ("sun", "sean", "amy"):
        assert _inbox(client, wedding[who]) == []
    assert pushes.ids == []


def test_a_new_item_is_announced_to_everyone_but_its_creator(client, wedding, pushes):
    item = wedding["add"](notify_members=True)
    assert _inbox(client, wedding["sun"]) == []
    for who in ("sean", "amy"):
        [entry] = _inbox(client, wedding[who])
        assert entry["title"] == "Wedding"
        assert entry["body"] == "Sun added Dinner on Apr 10"
        assert entry["category"] == "trip_activity"
        assert entry["type"] == "trip_item.created"
        assert entry["trip_id"] == wedding["trip"]["id"]
        assert entry["trip_item_id"] == item["id"]
        assert entry["actor"]["id"] == wedding["sun"].user_id
    assert len(pushes.ids) == 2


def test_a_private_item_is_only_announced_to_its_participants(client, wedding):
    wedding["add"](
        title="Ring shopping",
        visibility="private",
        participants=("sun", "sean"),
        notify_members=True,
    )
    assert _bodies(client, wedding["sean"]) == ["Sun added Ring shopping on Apr 10"]
    assert _inbox(client, wedding["amy"]) == []


def test_a_closed_item_is_announced_by_title_and_day(client, wedding):
    wedding["add"](
        title="Spa",
        visibility="closed",
        notes="Ask for Yuki",
        notify_members=True,
    )
    assert _bodies(client, wedding["amy"]) == ["Sun added Spa on Apr 10"]


def test_edits_say_what_changed(client, wedding):
    item = wedding["add"](participants=("sun", "sean"))
    wedding["edit"](item, starts_at="2026-04-11T19:30:00", notify_members=True)
    [entry] = _inbox(client, wedding["sean"])
    assert entry["type"] == "trip_item.updated"
    assert entry["body"] == "Sun moved Dinner to Apr 11 at 7:30 PM"
    assert entry["data"] == {"changes": ["time"]}
    assert entry["trip_item_id"] == item["id"]

    wedding["edit"](item, title="Supper", notes="Bring wine", notify_members=True)
    assert "Sun updated the name and notes of Supper" in _bodies(client, wedding["amy"])

    wedding["edit"](item, title="Rehearsal dinner", notify_members=True)
    assert "Sun renamed “Supper” to “Rehearsal dinner”" in _bodies(client, wedding["amy"])


def test_an_edit_that_changes_nothing_sends_nothing(client, wedding, pushes):
    item = wedding["add"]()
    wedding["edit"](
        item, title="Dinner", starts_at="2026-04-10T19:00:00", notify_members=True
    )
    assert _inbox(client, wedding["sean"]) == []
    assert pushes.ids == []


def test_closed_items_keep_their_details_from_non_participants(client, wedding):
    item = wedding["add"](title="Spa", visibility="closed", participants=("sun", "sean"))

    wedding["edit"](item, notes="Ask for Yuki", notify_members=True)
    assert _bodies(client, wedding["sean"]) == ["Sun updated the notes of Spa"]
    assert _inbox(client, wedding["amy"]) == []

    wedding["edit"](item, title="Massage", notes="Ask for Ken", notify_members=True)
    assert "Sun updated the name and notes of Massage" in _bodies(client, wedding["sean"])
    [for_amy] = _inbox(client, wedding["amy"])
    assert for_amy["body"] == "Sun renamed “Spa” to “Massage”"
    assert for_amy["data"] == {"changes": ["title"]}


def test_new_participants_hear_they_were_added(client, wedding):
    item = wedding["add"](
        title="Ring shopping", visibility="private", participants=("sun", "sean")
    )
    wedding["edit"](item, participants=("sun", "sean", "amy"), notify_members=True)
    [for_amy] = _inbox(client, wedding["amy"])
    assert for_amy["type"] == "trip_item.participant_added"
    assert for_amy["body"] == "Sun added you to Ring shopping on Apr 10"
    assert _bodies(client, wedding["sean"]) == ["Sun updated who's going to Ring shopping"]


def test_any_member_can_announce_their_change(client, wedding):
    item = wedding["add"](participants=("sun", "sean"))
    wedding["edit"](item, headers=wedding["sean"], notes="I'll book", notify_members=True)
    assert _bodies(client, wedding["sun"]) == ["Sean updated the notes of Dinner"]
    assert _inbox(client, wedding["sean"]) == []


def test_times_read_naturally():
    assert day_and_time(datetime(2026, 11, 12, 19, 0)) == "Nov 12 at 7 PM"
    assert day_and_time(datetime(2026, 11, 12, 7, 30)) == "Nov 12 at 7:30 AM"
    assert day_and_time(datetime(2026, 11, 12, 0, 5)) == "Nov 12 at 12:05 AM"
    assert day_and_time(datetime(2026, 11, 12, 12, 0)) == "Nov 12 at 12 PM"


def test_untitled_items_are_called_by_their_place(client, wedding):
    sun = wedding["sun"]

    def place(name):
        stub = client.post("/places", json={"google_place_id": f"g-{name}"}, headers=sun)
        return client.patch(f"/places/{stub.json()['id']}", json={"name": name}, headers=sun).json()

    ramen, sushi = place("Ichiran"), place("Sushi Dai")
    item = wedding["add"](title=None, place_id=ramen["id"], notify_members=True)
    assert _bodies(client, wedding["sean"]) == ["Sun added Ichiran on Apr 10"]

    wedding["edit"](item, place_id=sushi["id"], notify_members=True)
    wedding["edit"](item, title="Omakase", notify_members=True)
    assert set(_bodies(client, wedding["sean"])) == {
        "Sun added Ichiran on Apr 10",
        "Sun changed the place for Sushi Dai",
        "Sun renamed “Sushi Dai” to “Omakase”",
    }


def test_outsiders_hear_of_an_untitled_closed_item_by_its_kind(client, wedding):
    sun = wedding["sun"]
    stub = client.post("/places", json={"google_place_id": "g-Thermae"}, headers=sun)
    spa = client.patch(f"/places/{stub.json()['id']}", json={"name": "Thermae"}, headers=sun).json()

    item = wedding["add"](
        title=None, place_id=spa["id"], visibility="closed",
        participants=("sun", "sean"), notify_members=True,
    )
    wedding["edit"](item, title="Massage", notify_members=True)
    assert set(_bodies(client, wedding["sean"])) == {
        "Sun added Thermae on Apr 10",
        "Sun renamed “Thermae” to “Massage”",
    }
    assert set(_bodies(client, wedding["amy"])) == {
        "Sun added an activity on Apr 10",
        "Sun renamed an activity to “Massage”",
    }
