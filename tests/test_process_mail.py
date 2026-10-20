from sqlalchemy import func, select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_processing import (
    InboundEmail,
    extract_sender_email,
    record_inbound_emails,
)
from app.services.resend import ResendError


def _email(email_id: str, sender: str, **extra) -> dict:
    return {
        "id": email_id,
        "from": sender,
        "to": ["trips@inbound.outbound.example"],
        "subject": "Your flight to Tokyo is booked",
        "message_id": f"<{email_id}@airline.example>",
        "created_at": "2026-10-19T12:00:00.000Z",
        **extra,
    }


def _receipt_count(session) -> int:
    return session.scalar(select(func.count()).select_from(EmailReceipt))


def _receipt(session, email_id: str) -> EmailReceipt:
    return session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id)
    )


def test_process_mail_requires_auth(client):
    assert client.post("/trips/process-mail").status_code == 401


def test_matched_sender_is_pending_and_linked_to_account(client, make_user, resend, session):
    sun = make_user("sun")  # registered as sun@example.com
    resend.received = [_email("re_1", "sun@example.com")]

    body = client.post("/trips/process-mail", headers=sun).json()

    # Resend data returned untouched, plus the receipt.
    assert body["emails"] == resend.received
    info = body["receipts"]["re_1"]
    assert info["status"] == "pending"
    assert info["user_id"] == sun.user_id
    assert info["newly_recorded"] is True

    row = _receipt(session, "re_1")
    assert row.status is EmailReceiptStatus.PENDING
    assert str(row.user_id) == sun.user_id
    assert row.from_address == "sun@example.com"
    assert row.received_at is not None
    assert row.processed_at is None


def test_unmatched_sender_is_ignored(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [_email("re_2", "stranger@nowhere.example")]

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_2"]
    assert info["status"] == "ignored"
    assert info["user_id"] is None

    # Still recorded (audit + dedup), just not tied to an account.
    assert _receipt(session, "re_2").user_id is None


def test_sender_match_handles_display_name_and_casing(client, make_user, resend):
    sun = make_user("sun")
    resend.received = [_email("re_3", "Sun Chen <SUN@EXAMPLE.COM>")]

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_3"]
    assert info["status"] == "pending"
    assert info["user_id"] == sun.user_id


def test_mixed_batch_matches_only_the_known_sender(client, make_user, resend):
    sun = make_user("sun")
    resend.received = [
        _email("re_known", "sun@example.com"),
        _email("re_unknown", "someone@elsewhere.example"),
    ]

    receipts = client.post("/trips/process-mail", headers=sun).json()["receipts"]
    assert receipts["re_known"]["status"] == "pending"
    assert receipts["re_unknown"]["status"] == "ignored"


def test_second_pull_dedupes_without_reprocessing(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [_email("re_1", "sun@example.com")]

    client.post("/trips/process-mail", headers=sun)
    body = client.post("/trips/process-mail", headers=sun).json()

    info = body["receipts"]["re_1"]
    assert info["newly_recorded"] is False
    assert info["status"] == "pending"  # unchanged, pipeline not re-run
    assert _receipt_count(session) == 1


def test_empty_inbox_records_nothing(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = []

    body = client.post("/trips/process-mail", headers=sun).json()
    assert body == {"emails": [], "receipts": {}}
    assert _receipt_count(session) == 0


def test_process_mail_upstream_failure_is_502(client, make_user, resend):
    sun = make_user("sun")
    resend.error = ResendError("Resend returned 500: boom")
    assert client.post("/trips/process-mail", headers=sun).status_code == 502


def test_extract_sender_email_variants():
    assert extract_sender_email("Sun Chen <Sun@Example.com>") == "sun@example.com"
    assert extract_sender_email("PLAIN@Example.COM") == "plain@example.com"
    assert extract_sender_email(None) is None
    assert extract_sender_email("not-an-address") is None


def test_webhook_shape_reuses_the_same_processing(client, make_user, session):
    """An email.received payload normalizes to the same InboundEmail and runs the
    same record path — proving the pull and the future webhook share code."""
    sun = make_user("sun")
    data = {
        "email_id": "re_wh_1",
        "from": "sun@example.com",
        "subject": "Forwarded confirmation",
        "message_id": "<wh@airline.example>",
        "created_at": "2026-10-20T00:00:00.000Z",
    }
    inbound = InboundEmail.from_webhook(data)
    recorded = record_inbound_emails(session, [inbound])
    session.commit()

    result = recorded["re_wh_1"]
    assert result.newly_recorded is True
    assert result.receipt.status is EmailReceiptStatus.PENDING
    assert str(result.receipt.user_id) == sun.user_id
