"""GET /admin/notifications: the notification log across every inbox."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import NotificationCategory
from app.services.notifications import notify

C = NotificationCategory


def _notify(session, *users, **fields) -> int:
    fields = {"category": C.TRAVEL_ALERT, "type": "test", "title": "Hello", **fields}
    return notify(session, user_ids=[uuid.UUID(u.user_id) for u in users], **fields)


def _log(client, admin, **params) -> dict:
    response = client.get("/admin/notifications", headers=admin, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def sun(make_user):
    return make_user("sun")


@pytest.fixture
def sean(make_user):
    return make_user("sean")


def test_requires_admin_token(client, sun):
    assert client.get("/admin/notifications").status_code == 401
    assert client.get("/admin/notifications", headers=sun).status_code == 403


def test_lists_every_inbox_with_its_recipient(client, session, admin, sun, sean):
    start = datetime.now(UTC) - timedelta(hours=1)
    _notify(session, sun, title="For sun", visible_at=start)
    _notify(session, sean, title="For sean", visible_at=start + timedelta(minutes=1))

    body = _log(client, admin)
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert (body["limit"], body["offset"]) == (50, 0)
    # Newest first, each with whose inbox it's in.
    assert [(n["title"], n["recipient"]["email"]) for n in body["items"]] == [
        ("For sean", sean.email),
        ("For sun", sun.email),
    ]
    row = body["items"][0]
    assert row["read"] is False
    assert row["archived_at"] is None


def test_includes_scheduled_and_dismissed_rows(client, session, admin, sun):
    _notify(session, sun, title="Tomorrow", visible_at=datetime.now(UTC) + timedelta(days=1))
    _notify(session, sun, title="Dismissed")
    [dismissed] = client.get("/notifications", headers=sun).json()["items"]
    assert client.delete(f"/notifications/{dismissed['id']}", headers=sun).status_code == 204

    # Neither is in sun's inbox, but both are in the log.
    assert client.get("/notifications", headers=sun).json()["items"] == []
    rows = {n["title"]: n for n in _log(client, admin)["items"]}
    assert set(rows) == {"Tomorrow", "Dismissed"}
    assert rows["Dismissed"]["archived_at"] is not None


def test_paginates_with_total(client, session, admin, sun):
    start = datetime.now(UTC) - timedelta(hours=1)
    for n in range(5):
        _notify(session, sun, title=f"n{n}", visible_at=start + timedelta(minutes=n))

    pages = [_log(client, admin, limit=2, offset=offset) for offset in (0, 2, 4)]
    assert {page["total"] for page in pages} == {5}
    titles = [n["title"] for page in pages for n in page["items"]]
    assert titles == ["n4", "n3", "n2", "n1", "n0"]


def test_filters(client, session, admin, sun, sean):
    _notify(session, sun, title="Flight delayed", category=C.TRAVEL_ALERT)
    _notify(session, sun, title="Item edited", category=C.TRIP_ACTIVITY)
    _notify(session, sean, title="Flight boarding", category=C.TRAVEL_ALERT)

    def titles(**params):
        body = _log(client, admin, **params)
        assert body["total"] == len(body["items"])
        return sorted(n["title"] for n in body["items"])

    assert titles(q="FLIGHT") == ["Flight boarding", "Flight delayed"]  # title
    assert titles(q="sean@") == ["Flight boarding"]  # recipient email
    assert titles(user_id=sun.user_id) == ["Flight delayed", "Item edited"]
    assert titles(category="trip_activity") == ["Item edited"]
    assert titles(q="flight", user_id=sun.user_id) == ["Flight delayed"]
    assert titles(q="  ") == ["Flight boarding", "Flight delayed", "Item edited"]

    [delayed] = [n for n in _log(client, admin)["items"] if n["title"] == "Flight delayed"]
    assert client.post(f"/notifications/{delayed['id']}/read", headers=sun).status_code == 200
    assert titles(unread_only=True) == ["Flight boarding", "Item edited"]
