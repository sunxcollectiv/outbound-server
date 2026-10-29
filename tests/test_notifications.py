"""The notification groundwork: the inbox and its read state, notify(),
announcements, preferences and push devices."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Notification, NotificationCategory, Trip, TripItem, User
from app.services.notifications import notify, trip_audience

C = NotificationCategory


def _user(session, headers) -> User:
    return session.get(User, uuid.UUID(headers.user_id))


def _inbox(client, headers, **params) -> list[dict]:
    response = client.get("/notifications", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _titles(client, headers, **params) -> list[str]:
    return [n["title"] for n in _inbox(client, headers, **params)]


def _unread(client, headers) -> int:
    response = client.get("/notifications/unread-count", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["count"]


def _notify(session, *users, **fields) -> int:
    fields = {"category": C.TRAVEL_ALERT, "type": "test", "title": "Hello", **fields}
    return notify(session, user_ids=[uuid.UUID(u.user_id) for u in users], **fields)


@pytest.fixture
def sun(make_user):
    return make_user("sun")


@pytest.fixture
def sean(make_user):
    return make_user("sean")


# --- notify ---


def test_notify_puts_it_in_each_inbox_but_the_actors(client, session, sun, sean):
    sent = _notify(
        session,
        sun,
        sean,
        category=C.TRIP_ACTIVITY,
        type="trip_item.created",
        title="Sun added Dinner",
        body="Friday at 7",
        data={"trip_item_id": "x"},
        actor_id=uuid.UUID(sun.user_id),
    )
    assert sent == 1
    assert _inbox(client, sun) == []
    [entry] = _inbox(client, sean)
    assert entry["title"] == "Sun added Dinner"
    assert entry["body"] == "Friday at 7"
    assert entry["category"] == "trip_activity"
    assert entry["type"] == "trip_item.created"
    assert entry["data"] == {"trip_item_id": "x"}
    assert entry["actor"]["id"] == sun.user_id
    assert entry["read"] is False and entry["read_at"] is None


def test_notify_skips_who_switched_the_category_off(client, session, sun, sean):
    client.put(
        "/notifications/preferences",
        json=[{"category": "trip_activity", "channel": "in_app", "enabled": False}],
        headers=sean,
    )
    assert _notify(session, sun, sean, category=C.TRIP_ACTIVITY) == 1
    assert _notify(session, sun, sean, category=C.TRAVEL_ALERT) == 2
    assert [n["category"] for n in _inbox(client, sean)] == ["travel_alert"]


def test_a_dedupe_key_rewrites_instead_of_adding(client, session, sun):
    _notify(session, sun, title="Flight at 9", dedupe_key="flight:1")
    [first] = _inbox(client, sun)
    client.post(f"/notifications/{first['id']}/read", headers=sun)
    client.delete(f"/notifications/{first['id']}", headers=sun)
    assert _inbox(client, sun) == []

    _notify(session, sun, title="Flight now at 11", dedupe_key="flight:1")
    [again] = _inbox(client, sun)
    assert again["id"] == first["id"]
    assert again["title"] == "Flight now at 11"
    assert again["read"] is False

    _notify(session, sun, title="Another flight", dedupe_key="flight:2")
    assert len(_inbox(client, sun)) == 2


def test_scheduled_notifications_wait_until_due(client, session, sun):
    later = datetime.now(UTC) + timedelta(hours=1)
    _notify(session, sun, title="Flight tomorrow", visible_at=later)
    assert _inbox(client, sun) == []
    assert _unread(client, sun) == 0

    notification = session.query(Notification).one()
    assert client.post(f"/notifications/{notification.id}/read", headers=sun).status_code == 404

    notification.visible_at = datetime.now(UTC) - timedelta(minutes=1)
    session.flush()
    assert _titles(client, sun) == ["Flight tomorrow"]


def test_expired_notifications_drop_out(client, session, sun):
    _notify(session, sun, title="Gone", expires_at=datetime.now(UTC) - timedelta(hours=1))
    _notify(session, sun, title="Still here", expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert _titles(client, sun) == ["Still here"]
    assert _unread(client, sun) == 1


# --- the inbox ---


def test_inbox_pages_newest_first_without_repeats(client, session, sun):
    start = datetime.now(UTC) - timedelta(hours=1)
    for n in range(5):
        _notify(session, sun, title=f"n{n}", visible_at=start + timedelta(minutes=n))

    first = client.get("/notifications", params={"limit": 2}, headers=sun).json()
    assert [n["title"] for n in first["items"]] == ["n4", "n3"]
    second = client.get(
        "/notifications", params={"limit": 2, "before": first["next_cursor"]}, headers=sun
    ).json()
    assert [n["title"] for n in second["items"]] == ["n2", "n1"]
    last = client.get(
        "/notifications", params={"limit": 2, "before": second["next_cursor"]}, headers=sun
    ).json()
    assert [n["title"] for n in last["items"]] == ["n0"]
    assert last["next_cursor"] is None


def test_read_unread_and_read_all(client, session, sun):
    for title in ("a", "b", "c"):
        _notify(session, sun, title=title, category=C.TRAVEL_ALERT)
    _notify(session, sun, title="d", category=C.TRIP_ACTIVITY)
    assert _unread(client, sun) == 4
    a = next(n for n in _inbox(client, sun) if n["title"] == "a")

    read = client.post(f"/notifications/{a['id']}/read", headers=sun)
    assert read.status_code == 200 and read.json()["read"] is True
    assert _unread(client, sun) == 3
    assert sorted(_titles(client, sun, unread_only=True)) == ["b", "c", "d"]

    unread = client.delete(f"/notifications/{a['id']}/read", headers=sun)
    assert unread.status_code == 200 and unread.json()["read"] is False
    assert _unread(client, sun) == 4

    left = client.post("/notifications/read-all", params={"category": "travel_alert"}, headers=sun)
    assert left.json() == {"count": 1}
    assert client.post("/notifications/read-all", headers=sun).json() == {"count": 0}
    assert len(_inbox(client, sun)) == 4


def test_archiving_takes_it_out_of_the_inbox(client, session, sun):
    _notify(session, sun, title="Dismiss me")
    [entry] = _inbox(client, sun)
    assert client.delete(f"/notifications/{entry['id']}", headers=sun).status_code == 204
    assert _inbox(client, sun) == []
    assert _unread(client, sun) == 0


def test_other_peoples_notifications_are_not_found(client, session, sun, sean):
    _notify(session, sun)
    [entry] = _inbox(client, sun)
    for call in (
        client.post(f"/notifications/{entry['id']}/read", headers=sean),
        client.delete(f"/notifications/{entry['id']}/read", headers=sean),
        client.delete(f"/notifications/{entry['id']}", headers=sean),
    ):
        assert call.status_code == 404
    assert client.get(
        "/notifications", params={"before": entry["id"]}, headers=sean
    ).status_code == 422


# --- sending by hand ---


def test_admins_send_to_people_by_id_or_email(client, make_user, make_admin, sun, sean):
    admin = make_admin(make_user("admin"))
    payload = {
        "user_ids": [sun.user_id],
        "emails": [sean.email.upper(), admin.email],
        "category": "travel_alert",
        "title": "Your import failed",
        "data": {"receipt_id": "r1"},
    }
    sent = client.post("/notifications", json=payload, headers=admin)
    assert sent.status_code == 201, sent.text
    assert sent.json() == {"recipients": 3, "sent": 3}
    for who in (sun, sean, admin):
        [entry] = _inbox(client, who)
        assert entry["title"] == "Your import failed"
        assert entry["type"] == "message"
        assert entry["actor"] is None
        assert entry["data"] == {"receipt_id": "r1"}

    # Same key again: rewritten, not added.
    again = {**payload, "title": "Import fixed", "dedupe_key": "import:r1"}
    client.post("/notifications", json=again, headers=admin)
    client.post("/notifications", json=again, headers=admin)
    assert sorted(_titles(client, sun)) == ["Import fixed", "Your import failed"]


def test_sending_by_hand_is_for_admins_and_known_people(client, make_user, make_admin, sun):
    payload = {"user_ids": [sun.user_id], "category": "travel_alert", "title": "Hi"}
    assert client.post("/notifications", json=payload, headers=sun).status_code == 403

    admin = make_admin(make_user("admin"))
    nobody = {**payload, "emails": ["nobody@example.com"]}
    assert client.post("/notifications", json=nobody, headers=admin).status_code == 422
    stranger = {**payload, "user_ids": [str(uuid.uuid4())]}
    assert client.post("/notifications", json=stranger, headers=admin).status_code == 422
    bad_trip = {**payload, "trip_id": str(uuid.uuid4())}
    assert client.post("/notifications", json=bad_trip, headers=admin).status_code == 422
    empty = {"category": "travel_alert", "title": "Hi"}
    assert client.post("/notifications", json=empty, headers=admin).status_code == 422
    assert _inbox(client, sun) == []

    client.put(
        "/notifications/preferences",
        json=[{"category": "promotion", "channel": "in_app", "enabled": False}],
        headers=sun,
    )
    muted = {**payload, "category": "promotion"}
    assert client.post("/notifications", json=muted, headers=admin).json() == {
        "recipients": 1,
        "sent": 0,
    }


# --- announcements ---


def _announce(client, admin, **fields) -> dict:
    payload = {"category": "announcement", "title": "Hello everyone", **fields}
    response = client.post("/announcements", json=payload, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def admin(make_user, make_admin):
    return make_admin(make_user("admin"))


def test_only_admins_manage_announcements(client, sun, admin):
    payload = {"category": "announcement", "title": "Hi"}
    assert client.post("/announcements", json=payload, headers=sun).status_code == 403
    assert client.get("/announcements", headers=sun).status_code == 403
    assert client.get("/users/me", headers=sun).json()["is_admin"] is False
    assert client.get("/users/me", headers=admin).json()["is_admin"] is True


def test_announcements_reach_everyone_including_later_signups(
    client, make_user, sun, admin
):
    announcement = _announce(client, admin, body="New feature!")
    assert announcement["type"] == "announcement"
    assert _unread(client, sun) == 1
    [entry] = _inbox(client, sun)
    assert entry["announcement_id"] == announcement["id"]
    assert entry["body"] == "New feature!"

    newcomer = make_user("newcomer")
    assert _titles(client, newcomer) == ["Hello everyone"]
    # Reading again doesn't copy it twice.
    assert len(_inbox(client, newcomer)) == 1


def test_announcements_only_show_while_running(client, sun, admin):
    now = datetime.now(UTC)
    _announce(
        client,
        admin,
        title="Over",
        starts_at=(now - timedelta(hours=2)).isoformat(),
        ends_at=(now - timedelta(hours=1)).isoformat(),
    )
    _announce(client, admin, title="Soon", starts_at=(now + timedelta(hours=1)).isoformat())
    assert _inbox(client, sun) == []
    assert len(client.get("/announcements", headers=admin).json()) == 2


def test_ending_an_announcement_takes_it_out_of_inboxes(client, sun, admin):
    announcement = _announce(client, admin)
    assert _titles(client, sun) == ["Hello everyone"]
    ended = client.patch(
        f"/announcements/{announcement['id']}",
        json={"ends_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
        headers=admin,
    )
    # Ending before it started is refused.
    assert ended.status_code == 422
    announcement = _announce(
        client, admin, title="Sale", starts_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat()
    )
    assert "Sale" in _titles(client, sun)
    ended = client.patch(
        f"/announcements/{announcement['id']}",
        json={"ends_at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat()},
        headers=admin,
    )
    assert ended.status_code == 200, ended.text
    assert _titles(client, sun) == ["Hello everyone"]


def test_edits_and_deletes_follow_into_inboxes(client, sun, admin):
    announcement = _announce(client, admin, title="Typo")
    assert _titles(client, sun) == ["Typo"]
    fixed = client.patch(
        f"/announcements/{announcement['id']}", json={"title": "Fixed"}, headers=admin
    )
    assert fixed.status_code == 200, fixed.text
    assert _titles(client, sun) == ["Fixed"]

    gone = client.delete(f"/announcements/{announcement['id']}", headers=admin)
    assert gone.status_code == 204
    assert _inbox(client, sun) == []


def test_dismissed_announcements_stay_dismissed(client, sun, admin):
    _announce(client, admin)
    [entry] = _inbox(client, sun)
    client.delete(f"/notifications/{entry['id']}", headers=sun)
    assert _inbox(client, sun) == []
    assert _unread(client, sun) == 0


def test_promotions_can_be_switched_off_but_announcements_cannot(client, sun, admin):
    _announce(client, admin, category="promotion", title="50% off")
    _announce(client, admin, title="Maintenance tonight")
    client.put(
        "/notifications/preferences",
        json=[{"category": "promotion", "channel": "in_app", "enabled": False}],
        headers=sun,
    )
    assert _titles(client, sun) == ["Maintenance tonight"]
    refused = client.put(
        "/notifications/preferences",
        json=[{"category": "announcement", "channel": "in_app", "enabled": False}],
        headers=sun,
    )
    assert refused.status_code == 422


def test_broadcasts_are_announcements_or_promotions(client, admin):
    bad = client.post(
        "/announcements",
        json={"category": "trip_activity", "title": "Nope"},
        headers=admin,
    )
    assert bad.status_code == 422


# --- preferences ---


def test_preferences_default_and_round_trip(client, sun):
    def matrix():
        response = client.get("/notifications/preferences", headers=sun)
        assert response.status_code == 200, response.text
        return {(p["category"], p["channel"]): (p["enabled"], p["locked"]) for p in response.json()}

    defaults = matrix()
    assert len(defaults) == 12
    assert defaults[("promotion", "push")] == (False, False)
    assert defaults[("trip_activity", "push")] == (True, False)
    assert defaults[("announcement", "in_app")] == (True, True)

    response = client.put(
        "/notifications/preferences",
        json=[
            {"category": "promotion", "channel": "push", "enabled": True},
            {"category": "trip_activity", "channel": "push", "enabled": False},
            # Sending a locked switch as it is is fine.
            {"category": "announcement", "channel": "in_app", "enabled": True},
        ],
        headers=sun,
    )
    assert response.status_code == 200, response.text
    changed = matrix()
    assert changed[("promotion", "push")] == (True, False)
    assert changed[("trip_activity", "push")] == (False, False)
    assert changed[("travel_alert", "push")] == (True, False)

    client.put(
        "/notifications/preferences",
        json=[{"category": "trip_activity", "channel": "push", "enabled": True}],
        headers=sun,
    )
    assert matrix()[("trip_activity", "push")] == (True, False)


# --- push devices ---


def test_devices_register_by_token(client, sun, sean):
    payload = {"platform": "ios", "token": "tok-1", "app_version": "1.0", "timezone": "Asia/Tokyo"}
    first = client.post("/notifications/devices", json=payload, headers=sun)
    assert first.status_code == 200, first.text
    again = client.post(
        "/notifications/devices", json={**payload, "app_version": "1.1"}, headers=sun
    )
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["app_version"] == "1.1"
    assert "token" not in again.json()
    assert len(client.get("/notifications/devices", headers=sun).json()) == 1

    # The same phone signed into another account moves to it.
    moved = client.post("/notifications/devices", json=payload, headers=sean)
    assert moved.json()["id"] == first.json()["id"]
    assert client.get("/notifications/devices", headers=sun).json() == []

    device_id = moved.json()["id"]
    assert client.delete(f"/notifications/devices/{device_id}", headers=sun).status_code == 404
    assert client.delete(f"/notifications/devices/{device_id}", headers=sean).status_code == 204
    assert client.get("/notifications/devices", headers=sean).json() == []


def test_devices_need_a_real_timezone(client, sun):
    bad = client.post(
        "/notifications/devices",
        json={"platform": "android", "token": "t", "timezone": "Mars/Base"},
        headers=sun,
    )
    assert bad.status_code == 422


# --- trip_audience ---


def test_trip_audience_is_accounts_who_can_see_it(client, session, make_trip, sun, sean):
    trip = make_trip(
        sun, members=[{"email": sean.email}, {"email": "grandma@example.com"}]
    )
    ids = {m["email"].split("@")[0]: m["id"] for m in trip["members"]}
    url = f"/trips/{trip['id']}/items"

    def add(**fields):
        payload = {"kind": "activity", "title": "x", "starts_at": "2026-04-10T09:00:00", **fields}
        response = client.post(url, json=payload, headers=sun)
        assert response.status_code == 201, response.text
        return session.get(TripItem, uuid.UUID(response.json()["id"]))

    trip_row = session.get(Trip, uuid.UUID(trip["id"]))
    accounts = {uuid.UUID(sun.user_id), uuid.UUID(sean.user_id)}
    # Grandma has no account, so no inbox.
    assert set(trip_audience(session, trip_row)) == accounts

    private = add(
        visibility="private",
        participants=[{"member_id": ids["sun"]}, {"member_id": ids["grandma"]}],
    )
    assert trip_audience(session, trip_row, private) == [uuid.UUID(sun.user_id)]

    closed = add(visibility="closed", participants=[{"member_id": ids["sun"]}])
    assert set(trip_audience(session, trip_row, closed)) == accounts
