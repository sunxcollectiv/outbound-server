from sqlalchemy import func, select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_processing import extract_sender_email
from app.services.resend import ResendError


def _email(email_id: str, sender: str, **extra) -> dict:
    return {
        "id": email_id,
        "from": sender,
        "to": ["trips@inbound.outbound.example"],
        "subject": "Your flight to Tokyo is booked",
        "message_id": f"<{email_id}@airline.example>",
        "created_at": "2026-10-19T12:00:00.000Z",
        "text": "Confirmation ABC123. NRT 2026-04-09.",
        "html": "<p>Confirmation <b>ABC123</b></p>",
        **extra,
    }


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(EmailReceipt))


def _receipt(session, email_id: str) -> EmailReceipt:
    return session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id)
    )


def test_retrieve_endpoints_require_auth(client):
    assert client.post("/received/mail").status_code == 401
    assert client.post("/received/mail/re_1").status_code == 401


def test_retrieve_recent_stores_metadata_only(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [_email("re_1", "a@x.example"), _email("re_2", "b@y.example")]

    body = client.post("/received/mail", headers=sun).json()
    assert {r["resend_email_id"] for r in body} == {"re_1", "re_2"}
    assert all(r["status"] == "received" for r in body)
    assert all(r["raw"] is None for r in body)  # list carries no body

    row = _receipt(session, "re_1")
    assert row.status is EmailReceiptStatus.RECEIVED
    assert row.from_address == "a@x.example"
    assert row.subject == "Your flight to Tokyo is booked"
    assert row.raw is None
    # Pure retrieval: no sender match, no extraction.
    assert row.user_id is None
    assert row.extracted is None


def test_retrieve_recent_respects_limit(client, make_user, resend):
    sun = make_user("sun")
    resend.received = [_email(f"re_{i}", "a@x.example") for i in range(10)]

    body = client.post("/received/mail?limit=3", headers=sun).json()
    assert len(body) == 3


def test_retrieve_one_stores_the_raw_body(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [_email("re_1", "a@x.example")]

    body = client.post("/received/mail/re_1", headers=sun).json()
    assert body["resend_email_id"] == "re_1"
    assert body["status"] == "received"
    assert body["raw"]["text"] == "Confirmation ABC123. NRT 2026-04-09."
    assert body["raw"]["html"] == "<p>Confirmation <b>ABC123</b></p>"

    row = _receipt(session, "re_1")
    assert row.raw["id"] == "re_1"
    assert resend.fetched == ["re_1"]  # fetched the single email by id


def test_retrieval_is_idempotent_and_backfills_the_body(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [_email("re_1", "a@x.example")]

    # Seen first via the list (metadata only, no body)...
    client.post("/received/mail", headers=sun)
    assert _receipt(session, "re_1").raw is None
    assert _count(session) == 1

    # ...then fetched by id: same row, now with its body, no duplicate.
    client.post("/received/mail/re_1", headers=sun)
    session.expire_all()
    assert _count(session) == 1
    assert _receipt(session, "re_1").raw is not None


def test_retrieve_recent_upstream_failure_is_502(client, make_user, resend):
    sun = make_user("sun")
    resend.error = ResendError("Resend returned 500: boom")
    assert client.post("/received/mail", headers=sun).status_code == 502


def test_extract_sender_email_variants():
    assert extract_sender_email("Sun Chen <Sun@Example.com>") == "sun@example.com"
    assert extract_sender_email("PLAIN@Example.COM") == "plain@example.com"
    assert extract_sender_email(None) is None
    assert extract_sender_email("not-an-address") is None
