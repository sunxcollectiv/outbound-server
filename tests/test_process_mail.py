from sqlalchemy import func, select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_extraction import Extraction, MailExtractionError
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


def test_matched_booking_is_processed_and_extraction_stored(
    client, make_user, resend, mail_extractor, session
):
    sun = make_user("sun")  # registered as sun@example.com
    resend.received = [_email("re_1", "sun@example.com")]

    body = client.post("/trips/process-mail", headers=sun).json()
    assert body["emails"] == resend.received

    info = body["receipts"]["re_1"]
    assert info["status"] == "processed"
    assert info["user_id"] == sun.user_id
    assert info["category"] == "flight"
    assert info["newly_recorded"] is True

    row = _receipt(session, "re_1")
    assert row.status is EmailReceiptStatus.PROCESSED
    assert row.processed_at is not None
    assert row.extracted["category"] == "flight"
    assert row.extracted["items"][0]["title"] == "Flight to Tokyo"
    assert row.failures == []
    # The body was fetched to feed the extractor.
    assert resend.fetched == ["re_1"]


def test_unmatched_sender_is_ignored_and_never_calls_claude(
    client, make_user, resend, mail_extractor, session
):
    make_user("sun")
    sun = make_user("other")
    resend.received = [_email("re_2", "stranger@nowhere.example")]

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_2"]
    assert info["status"] == "ignored"
    assert info["user_id"] is None
    assert info["category"] is None

    # No account → no body fetch and no (paid) extraction call.
    assert resend.fetched == []
    assert mail_extractor.calls == []
    assert _receipt(session, "re_2").extracted is None


def test_not_a_booking_is_rejected(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")
    resend.received = [_email("re_3", "sun@example.com")]
    mail_extractor.result = Extraction(
        category="not_a_booking", confidence=0.99, summary="Newsletter", items=[]
    )

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_3"]
    assert info["status"] == "rejected"
    # Still stored, so we can see why it was rejected.
    assert _receipt(session, "re_3").extracted["category"] == "not_a_booking"


def test_low_confidence_is_rejected(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")
    resend.received = [_email("re_4", "sun@example.com")]
    mail_extractor.result = Extraction(
        category="flight", confidence=0.2, summary="Maybe a flight?", items=[]
    )

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_4"]
    assert info["status"] == "rejected"


def test_extractor_error_leaves_pending_with_failure_logged(
    client, make_user, resend, mail_extractor, session
):
    sun = make_user("sun")
    resend.received = [_email("re_5", "sun@example.com")]
    mail_extractor.error = MailExtractionError("Claude extraction failed: boom")

    info = client.post("/trips/process-mail", headers=sun).json()["receipts"]["re_5"]
    assert info["status"] == "pending"

    row = _receipt(session, "re_5")
    assert len(row.failures) == 1
    assert row.failures[0]["step"] == "extract"
    assert row.extracted is None


def test_second_pull_dedupes_without_reprocessing(
    client, make_user, resend, mail_extractor, session
):
    sun = make_user("sun")
    resend.received = [_email("re_1", "sun@example.com")]

    client.post("/trips/process-mail", headers=sun)
    assert len(mail_extractor.calls) == 1

    body = client.post("/trips/process-mail", headers=sun).json()
    info = body["receipts"]["re_1"]
    assert info["newly_recorded"] is False
    assert info["status"] == "processed"
    # Already recorded → pipeline (and the paid Claude call) does not run again.
    assert len(mail_extractor.calls) == 1
    assert _receipt_count(session) == 1


def test_empty_inbox_records_nothing(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = []

    body = client.post("/trips/process-mail", headers=sun).json()
    assert body == {"emails": [], "receipts": {}}
    assert _receipt_count(session) == 0


def test_process_mail_upstream_list_failure_is_502(client, make_user, resend):
    sun = make_user("sun")
    resend.error = ResendError("Resend returned 500: boom")
    assert client.post("/trips/process-mail", headers=sun).status_code == 502


def test_extract_sender_email_variants():
    assert extract_sender_email("Sun Chen <Sun@Example.com>") == "sun@example.com"
    assert extract_sender_email("PLAIN@Example.COM") == "plain@example.com"
    assert extract_sender_email(None) is None
    assert extract_sender_email("not-an-address") is None
