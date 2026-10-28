"""Push: what notify() hands over on commit, what deliver() sends to Apple,
and the APNs client itself."""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings
from app.models import Notification, NotificationCategory, PushDevice
from app.services import push
from app.services.apns import ApnsClient, ApnsResponse, build_apns_client, collapse_id
from app.services.notifications import notify
from app.services.push import deliver

C = NotificationCategory


@pytest.fixture
def sun(make_user):
    return make_user("sun")


def _notify(session, *users, **fields) -> int:
    fields = {"category": C.TRAVEL_ALERT, "type": "test", "title": "Hello", **fields}
    return notify(session, user_ids=[uuid.UUID(u.user_id) for u in users], **fields)


def _device(client, headers, token, platform="ios") -> dict:
    response = client.post(
        "/notifications/devices", json={"platform": platform, "token": token}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ids(session) -> list[uuid.UUID]:
    return [n.id for n in session.query(Notification).order_by(Notification.created_at)]


# --- queued on commit ---


def test_pushes_go_out_only_once_the_session_commits(session, pushes, sun):
    _notify(session, sun)
    assert pushes.ids == []
    session.commit()
    assert pushes.ids == _ids(session)
    session.commit()
    assert len(pushes.batches) == 1


def test_a_rollback_sends_nothing(session, pushes, sun):
    _notify(session, sun)
    session.rollback()
    session.commit()
    assert pushes.ids == []
    assert _ids(session) == []


def test_scheduled_notifications_are_not_pushed(session, pushes, sun):
    _notify(session, sun, visible_at=datetime.now(UTC) + timedelta(hours=1))
    session.commit()
    assert pushes.ids == []


def test_a_rewrite_pushes_the_existing_row(session, pushes, sun):
    _notify(session, sun, dedupe_key="flight:1")
    session.commit()
    _notify(session, sun, title="Changed", dedupe_key="flight:1")
    session.commit()
    [row] = _ids(session)
    assert pushes.batches == [[row], [row]]


def test_sending_by_hand_pushes(client, make_user, make_admin, pushes, sun):
    admin = make_admin(make_user("admin"))
    payload = {"emails": [sun.email, admin.email], "category": "travel_alert", "title": "Hi"}
    assert client.post("/notifications", json=payload, headers=admin).status_code == 201
    assert len(pushes.ids) == 2


# --- delivery ---


def test_delivery_sends_the_payload_the_app_expects(client, session, make_trip, pushes, apns, sun):
    trip = make_trip(sun)
    _device(client, sun, "ios-token")
    _device(client, sun, "android-token", platform="android")
    _notify(session, sun, title="Older")
    _notify(
        session,
        sun,
        title="Gate change",
        body="Now boarding at 52",
        trip_id=uuid.UUID(trip["id"]),
        dedupe_key="gate:1",
    )
    session.commit()
    latest = session.query(Notification).filter_by(title="Gate change").one()

    assert deliver(session, [latest.id], apns) == 1
    [(token, payload, collapse_key)] = apns.sent
    assert token == "ios-token"
    assert collapse_key == "gate:1"
    assert payload == {
        "aps": {
            "alert": {"title": "Gate change", "body": "Now boarding at 52"},
            "sound": "default",
            "badge": 2,
        },
        "notification_id": str(latest.id),
        "trip_id": trip["id"],
    }


def test_delivery_leaves_out_what_a_notification_lacks(client, session, pushes, apns, sun):
    _device(client, sun, "ios-token")
    _notify(session, sun, title="Just a title")
    session.commit()
    deliver(session, pushes.ids, apns)
    [(_, payload, collapse_key)] = apns.sent
    assert payload == {
        "aps": {"alert": {"title": "Just a title"}, "sound": "default", "badge": 1},
        "notification_id": str(pushes.ids[0]),
    }
    assert collapse_key is None


def test_delivery_respects_push_preferences(client, session, pushes, apns, sun):
    _device(client, sun, "ios-token")
    client.put(
        "/notifications/preferences",
        json=[{"category": "travel_alert", "channel": "push", "enabled": False}],
        headers=sun,
    )
    _notify(session, sun, category=C.TRAVEL_ALERT)
    # Promotions are push-off by default.
    _notify(session, sun, category=C.PROMOTION, title="Sale")
    _notify(session, sun, category=C.TRIP_ACTIVITY, title="Sean added dinner")
    session.commit()
    assert len(pushes.ids) == 3
    deliver(session, pushes.ids, apns)
    assert [payload["aps"]["alert"]["title"] for _, payload, _ in apns.sent] == [
        "Sean added dinner"
    ]


def test_delivery_skips_rows_that_are_gone_dismissed_or_not_due(
    client, session, pushes, apns, sun
):
    _device(client, sun, "ios-token")
    _notify(session, sun, title="Dismissed")
    session.commit()
    session.query(Notification).one().archived_at = datetime.now(UTC)
    session.flush()
    later = datetime.now(UTC) + timedelta(hours=1)
    _notify(session, sun, title="Later", visible_at=later)
    session.flush()
    everything = _ids(session) + [uuid.uuid4()]
    assert deliver(session, everything, apns) == 0
    assert apns.sent == []


def test_dead_tokens_are_forgotten_and_failures_dont_raise(client, session, pushes, apns, sun):
    for token in ("gone", "bad", "flaky", "offline", "fine"):
        _device(client, sun, token)
    apns.responses = {
        "gone": ApnsResponse(410, "Unregistered"),
        "bad": ApnsResponse(400, "BadDeviceToken"),
        "flaky": ApnsResponse(503, "ServiceUnavailable"),
        "offline": httpx.ConnectError("no route to Apple"),
    }
    _notify(session, sun, title="One")
    _notify(session, sun, title="Two")
    session.commit()

    assert deliver(session, pushes.ids, apns) == 2  # "fine", twice
    left = {d.token for d in session.query(PushDevice)}
    assert left == {"flaky", "offline", "fine"}
    # A dead token isn't tried again for the second notification.
    assert [token for token, _, _ in apns.sent].count("gone") == 1


def test_without_apns_configured_nothing_is_sent(monkeypatch):
    submitted = []
    monkeypatch.setattr(push, "get_apns_client", lambda: None)
    monkeypatch.setattr(push._executor, "submit", lambda fn: submitted.append(fn))
    push._deliver_in_background([uuid.uuid4()])
    assert submitted == []


# --- the APNs client ---


@pytest.fixture
def p8() -> tuple[str, ec.EllipticCurvePublicKey]:
    """A throwaway key in the .p8 format Apple hands out."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def _client(pem, handler) -> ApnsClient:
    http = httpx.Client(
        base_url="https://api.sandbox.push.apple.com", transport=httpx.MockTransport(handler)
    )
    return ApnsClient(
        key_id="KEY123", team_id="TEAM456", private_key=pem, topic="earth.outbound", http=http
    )


def test_client_signs_and_addresses_the_request(p8):
    pem, public_key = p8
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = _client(pem, handler)
    response = client.send("abc123", {"aps": {"alert": {"title": "Hi"}}}, collapse_key="k")
    assert response.ok

    [request] = seen
    assert request.url.path == "/3/device/abc123"
    assert request.headers["apns-topic"] == "earth.outbound"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["apns-collapse-id"] == "k"
    assert json.loads(request.content) == {"aps": {"alert": {"title": "Hi"}}}

    scheme, token = request.headers["authorization"].split(" ")
    assert scheme == "bearer"
    assert jwt.get_unverified_header(token)["kid"] == "KEY123"
    claims = jwt.decode(token, public_key, algorithms=["ES256"])
    assert claims["iss"] == "TEAM456"

    # The provider token is reused, not re-signed per push.
    client.send("abc123", {})
    assert seen[1].headers["authorization"] == request.headers["authorization"]


def test_client_reports_apples_reason(p8):
    pem, _ = p8
    client = _client(pem, lambda r: httpx.Response(400, json={"reason": "BadDeviceToken"}))
    response = client.send("abc", {})
    assert (response.status, response.reason, response.token_is_dead) == (
        400,
        "BadDeviceToken",
        True,
    )
    assert not ApnsResponse(400, "PayloadTooLarge").token_is_dead
    assert ApnsResponse(410, "Unregistered").token_is_dead


def test_client_renews_an_expired_provider_token_once(p8, monkeypatch):
    pem, _ = p8
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
        return httpx.Response(200)

    client = _client(pem, handler)
    # Apple disagrees about the clock; the retry is signed later, so it differs.
    clock = itertools.count(1_000_000, 100)
    monkeypatch.setattr("app.services.apns.time.time", lambda: float(next(clock)))
    assert client.send("abc", {}).ok
    assert len(seen) == 2 and seen[0] != seen[1]


def test_long_collapse_keys_are_hashed_to_fit():
    assert collapse_id("short") == "short"
    long = collapse_id("x" * 100)
    assert len(long) == 64 and long == collapse_id("x" * 100)


def test_client_is_built_only_when_configured(p8, tmp_path):
    pem, _ = p8
    base = {"_env_file": None, "apns_key_id": "KEY123", "apns_team_id": "TEAM456"}
    assert build_apns_client(Settings(_env_file=None)) is None
    assert build_apns_client(Settings(**base)) is None

    one_line = pem.replace("\n", "\\n")
    inline = build_apns_client(Settings(**base, apns_private_key=one_line))
    assert inline is not None and inline._private_key == pem

    key_file = tmp_path / "AuthKey_KEY123.p8"
    key_file.write_text(pem)
    from_file = build_apns_client(
        Settings(**base, apns_key_path=str(key_file), apns_use_sandbox=False)
    )
    assert from_file is not None
    assert str(from_file._http.base_url) == "https://api.push.apple.com"
