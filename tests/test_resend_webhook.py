import base64
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.api.webhooks import get_resend_webhook_secret, get_session_factory
from app.main import app
from app.models import EmailReceipt
from app.services import resend_webhook
from app.services.mail_extraction import (
    ExtractedItem,
    ExtractedPlace,
    ExtractedSegment,
    Extraction,
    MailExtractionError,
)
from app.services.mail_ingest import process_email

SECRET = "whsec_" + base64.b64encode(b"a test signing secret").decode()

# JAPAN_TRIP (conftest make_trip default) runs 2026-04-09 .. 2026-04-15.
FLIGHT = Extraction(
    category="flight", confidence=0.95, summary="LAX to Tokyo",
    items=[ExtractedItem(
        kind="transit", transit_mode="flight", title="Flight LAX to Tokyo",
        confirmation_number="ABC123",
        segments=[ExtractedSegment(
            transit_mode="flight", carrier="ANA", flight_number="NH105",
            start_local="2026-04-09T11:00", start_timezone="America/Los_Angeles",
            end_local="2026-04-10T15:00", end_timezone="Asia/Tokyo",
            origin=ExtractedPlace(name="LAX", iata_code="LAX", city="Los Angeles"),
            destination=ExtractedPlace(name="HND", iata_code="HND", city="Tokyo"),
        )],
    )],
)


@pytest.fixture
def webhook(client, session, clock):
    """Posts a signed Resend event the way Svix would. The background run
    shares the test's session (and so its rolled-back transaction). Keyword
    arguments bend the call: `secret` to sign with another key, `sent_at` to
    date the signature, `headers` to replace them outright."""
    app.dependency_overrides[get_resend_webhook_secret] = lambda: SECRET
    app.dependency_overrides[get_session_factory] = lambda: lambda: nullcontext(session)

    def _post(event, *, secret=SECRET, sent_at=None, headers=None):
        body = event if isinstance(event, bytes) else json.dumps(event).encode()
        if headers is None:
            timestamp = str(int((sent_at or clock.now).timestamp()))
            signature = resend_webhook.sign(secret, "msg_1", timestamp, body)
            headers = {
                "svix-id": "msg_1",
                "svix-timestamp": timestamp,
                "svix-signature": f"v1,{signature}",
            }
        return client.post(
            "/webhooks/resend", content=body, headers={"content-type": "application/json", **headers}
        )

    return _post


def _received(email_id: str, sender: str) -> dict:
    """An `email.received` event: metadata only, as Resend sends it."""
    return {
        "type": "email.received",
        "created_at": "2026-03-01T12:00:01.000Z",
        "data": {
            "email_id": email_id,
            "created_at": "2026-03-01T12:00:00.000Z",
            "from": sender,
            "to": ["trips@inbound.wravel.example"],
            "subject": "Your flight is booked",
            "message_id": f"<{email_id}@airline.example>",
            "attachments": [],
        },
    }


def _stored_email(email_id: str, sender: str) -> dict:
    """The same email as Resend's retrieve endpoint returns it, body and all."""
    return {
        "id": email_id, "from": sender, "subject": "Your flight is booked",
        "message_id": f"<{email_id}@airline.example>",
        "created_at": "2026-03-01T12:00:00.000Z",
        "text": "Confirmation ABC123", "html": "<p>Confirmation ABC123</p>",
    }


def _receipt(session, email_id: str) -> EmailReceipt:
    return session.scalar(select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id))


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(EmailReceipt))


# --- the signature ---


def test_sign_matches_the_svix_documented_vector():
    # https://docs.svix.com/receiving/verifying-payloads/how-manual
    signature = resend_webhook.sign(
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "msg_p5jXN8AQM9LWM0D4loKWxJek",
        "1614265330",
        b'{"test": 2432232314}',
    )
    assert signature == "g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_verify_accepts_any_matching_entry_among_several():
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    timestamp = str(int(now.timestamp()))
    good = resend_webhook.sign(SECRET, "msg_1", timestamp, b"{}")
    headers = {
        "svix-id": "msg_1",
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,bm90IGl0 v2,{good} v1,{good}",
    }
    resend_webhook.verify(SECRET, headers, b"{}", now=now)
    with pytest.raises(resend_webhook.WebhookSignatureError):
        resend_webhook.verify(SECRET, headers, b'{"tampered": true}', now=now)


def test_unsigned_call_is_401(client, webhook, session):
    r = webhook(_received("re_1", "sun@example.com"), headers={})
    assert r.status_code == 401
    assert _count(session) == 0


def test_wrong_secret_is_401(webhook, session):
    other = "whsec_" + base64.b64encode(b"someone else's secret").decode()
    r = webhook(_received("re_1", "sun@example.com"), secret=other)
    assert r.status_code == 401
    assert _count(session) == 0


@pytest.mark.parametrize("skew", [timedelta(minutes=-6), timedelta(minutes=6)])
def test_stale_or_future_timestamp_is_401(webhook, clock, session, skew):
    r = webhook(_received("re_1", "sun@example.com"), sent_at=clock.now + skew)
    assert r.status_code == 401
    assert _count(session) == 0


def test_unconfigured_secret_is_503(client, webhook):
    app.dependency_overrides[get_resend_webhook_secret] = lambda: None
    assert webhook(_received("re_1", "sun@example.com")).status_code == 503


# --- what a verified event does ---


def test_received_email_lands_in_the_senders_trip(
    webhook, client, make_user, make_trip, resend, mail_extractor, session
):
    sun = make_user("sun")
    trip = make_trip(sun)
    resend.received = [_stored_email("re_1", "Sun <sun@example.com>")]
    mail_extractor.result = FLIGHT

    r = webhook(_received("re_1", "Sun <sun@example.com>"))
    assert r.status_code == 200 and r.json() == {"status": "accepted"}

    receipt = _receipt(session, "re_1")
    assert receipt.status.value == "processed"
    assert str(receipt.trip_id) == trip["id"]
    assert receipt.raw["text"] == "Confirmation ABC123"  # body fetched from Resend
    assert resend.fetched == ["re_1"]

    items = client.get(f"/trips/{trip['id']}/items", headers=sun).json()
    assert [i["confirmation_number"] for i in items] == ["ABC123"]
    assert items[0]["source"] == "email"


def test_redelivery_does_not_import_twice(
    webhook, client, make_user, make_trip, resend, mail_extractor, session
):
    sun = make_user("sun")
    trip = make_trip(sun)
    resend.received = [_stored_email("re_1", "sun@example.com")]
    mail_extractor.result = FLIGHT

    for _ in range(2):
        assert webhook(_received("re_1", "sun@example.com")).status_code == 200

    assert _count(session) == 1
    assert len(mail_extractor.calls) == 1
    assert len(client.get(f"/trips/{trip['id']}/items", headers=sun).json()) == 1


def test_unknown_sender_is_stored_and_ignored(webhook, resend, mail_extractor, session):
    r = webhook(_received("re_1", "stranger@example.com"))
    assert r.status_code == 200
    assert _receipt(session, "re_1").status.value == "ignored"
    assert resend.fetched == [] and mail_extractor.calls == []


def test_no_matching_trip_stays_extracted(webhook, make_user, resend, mail_extractor, session):
    make_user("sun")  # no trips
    resend.received = [_stored_email("re_1", "sun@example.com")]
    mail_extractor.result = FLIGHT

    assert webhook(_received("re_1", "sun@example.com")).status_code == 200
    receipt = _receipt(session, "re_1")
    assert receipt.status.value == "extracted" and receipt.trip_id is None


def test_extraction_failure_is_recorded_and_still_acknowledged(
    webhook, make_user, resend, mail_extractor, session
):
    make_user("sun")
    resend.received = [_stored_email("re_1", "sun@example.com")]
    mail_extractor.error = MailExtractionError("Claude is down")

    assert webhook(_received("re_1", "sun@example.com")).status_code == 200
    receipt = _receipt(session, "re_1")
    assert receipt.status.value == "pending"
    assert len(receipt.failures) == 1


def test_a_crash_in_the_background_run_leaves_the_receipt_for_later(
    webhook, make_user, resend, mail_extractor, session
):
    make_user("sun")
    resend.received = [_stored_email("re_1", "sun@example.com")]
    mail_extractor.error = RuntimeError("something unexpected")

    assert webhook(_received("re_1", "sun@example.com")).status_code == 200
    assert _receipt(session, "re_1").status.value == "received"


def test_other_event_types_are_acknowledged_and_ignored(webhook, session):
    r = webhook({"type": "email.delivered", "data": {"email_id": "se_1"}})
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert _count(session) == 0


def test_event_without_an_email_id_is_400(webhook, session):
    assert webhook({"type": "email.received", "data": {}}).status_code == 400
    assert webhook({"type": "email.received"}).status_code == 400
    assert webhook(b"not json").status_code == 400
    assert _count(session) == 0


def test_process_email_unknown_id_is_none(session, resend, mail_extractor):
    assert process_email(session, "re_missing", resend=resend, extractor=mail_extractor) is None
