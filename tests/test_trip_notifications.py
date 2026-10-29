"""An organizer's trip edits, optionally announced to the other members."""

from datetime import date

import pytest

from app.services.trip_notifications import date_range


def _inbox(client, headers) -> list[dict]:
    response = client.get("/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


@pytest.fixture
def japan(client, make_user, make_trip):
    """Sun organizes Japan; Sean and Amy are on it, and Grandma has no account."""
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(
        sun,
        members=[{"email": sean.email}, {"email": amy.email}, {"email": "grandma@example.com"}],
    )

    def edit(headers=sun, **changes):
        response = client.patch(f"/trips/{trip['id']}", json=changes, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    return {"trip": trip, "sun": sun, "sean": sean, "amy": amy, "edit": edit}


def test_edits_are_quiet_unless_asked(client, japan, pushes):
    japan["edit"](title="Japan 2027")
    japan["edit"](title="Japan 2028", notify_members=False)
    for who in ("sun", "sean", "amy"):
        assert _inbox(client, japan[who]) == []
    assert pushes.ids == []


def test_members_but_not_the_editor_hear_about_a_rename(client, japan, pushes):
    japan["edit"](title="Japan 2027", notify_members=True)
    assert _inbox(client, japan["sun"]) == []
    for who in ("sean", "amy"):
        [entry] = _inbox(client, japan[who])
        assert entry["title"] == "Japan 2027"
        assert entry["body"] == "Sun renamed the trip from “Japan”"
        assert entry["category"] == "trip_activity"
        assert entry["type"] == "trip.updated"
        assert entry["data"] == {"changes": ["title"]}
        assert entry["trip_id"] == japan["trip"]["id"]
        assert entry["actor"]["id"] == japan["sun"].user_id
    # Both get a push once the edit commits.
    assert len(pushes.ids) == 2


def test_date_changes_say_the_new_dates(client, japan):
    japan["edit"](start_date="2026-04-28", end_date="2026-05-03", notify_members=True)
    [entry] = _inbox(client, japan["sean"])
    assert entry["body"] == "Sun changed the dates to Apr 28 – May 3, 2026"


def test_several_changes_are_listed(client, japan):
    japan["edit"](
        title="Japan 2027",
        end_date="2026-04-20",
        description="Cherry blossoms",
        notify_members=True,
    )
    [entry] = _inbox(client, japan["sean"])
    assert entry["body"] == "Sun updated the name, dates and description"
    assert entry["data"] == {"changes": ["title", "dates", "description"]}


def test_an_edit_that_changes_nothing_sends_nothing(client, japan, pushes):
    japan["edit"](title="Japan", start_date="2026-04-09", notify_members=True)
    assert _inbox(client, japan["sean"]) == []
    assert pushes.ids == []


def test_roster_changes_tell_who_was_added_and_not_who_was_removed(
    client, make_user, japan
):
    lee = make_user("lee")
    trip = japan["trip"]
    roster = [
        {"email": m["email"], "role": m["role"]}
        for m in trip["members"]
        if m["email"] != japan["amy"].email
    ]
    japan["edit"](members=[*roster, {"email": lee.email}], notify_members=True)

    [for_sean] = _inbox(client, japan["sean"])
    assert for_sean["body"] == "Sun updated who's going"
    [for_lee] = _inbox(client, lee)
    assert for_lee["type"] == "trip.member_added"
    assert for_lee["body"] == "Sun added you to the trip"
    assert for_lee["trip_id"] == trip["id"]
    assert _inbox(client, japan["amy"]) == []


def test_another_organizer_editing_notifies_the_owner(client, japan, set_role):
    set_role(japan["trip"]["id"], japan["sun"], japan["sean"].email, "organizer")
    japan["edit"](headers=japan["sean"], title="Sean's Japan", notify_members=True)
    [for_sun] = _inbox(client, japan["sun"])
    assert for_sun["body"] == "Sean renamed the trip from “Japan”"
    assert _inbox(client, japan["sean"]) == []


def test_date_ranges_read_naturally():
    assert date_range(date(2026, 4, 9), date(2026, 4, 9)) == "Apr 9, 2026"
    assert date_range(date(2026, 4, 9), date(2026, 4, 15)) == "Apr 9 – 15, 2026"
    assert date_range(date(2026, 4, 28), date(2026, 5, 3)) == "Apr 28 – May 3, 2026"
    assert date_range(date(2026, 12, 28), date(2027, 1, 3)) == "Dec 28, 2026 – Jan 3, 2027"
