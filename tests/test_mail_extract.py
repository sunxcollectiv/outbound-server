from sqlalchemy import select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_extraction import Extraction, MailExtractionError


def _email(email_id: str, sender: str, **extra) -> dict:
    return {
        "id": email_id,
        "from": sender,
        "subject": "Your flight to Tokyo is booked",
        "message_id": f"<{email_id}@airline.example>",
        "created_at": "2026-10-19T12:00:00.000Z",
        "text": "Confirmation ABC123. NRT 2026-04-09.",
        "html": "<p>ABC123</p>",
        **extra,
    }


def _receipt(session, email_id: str) -> EmailReceipt:
    return session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id)
    )


def _retrieve_list(client, headers, resend, emails):
    resend.received = emails
    assert client.post("/received/mail", headers=headers).status_code == 200


def test_extract_endpoints_require_auth(client):
    assert client.post("/received/mail/extract").status_code == 401
    assert client.post("/received/mail/extract/re_1").status_code == 401


def test_extract_one_processes_a_matched_booking(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")  # sun@example.com
    _retrieve_list(client, sun, resend, [_email("re_1", "sun@example.com")])

    body = client.post("/received/mail/extract/re_1", headers=sun).json()
    assert body["status"] == "extracted"
    assert body["user_id"] == sun.user_id
    assert body["extracted"]["category"] == "flight"
    assert body["extracted_at"] is not None

    row = _receipt(session, "re_1")
    assert row.status is EmailReceiptStatus.EXTRACTED
    assert row.extracted_at is not None
    assert row.raw is not None  # fetched + backfilled during extract
    assert "re_1" in resend.fetched


def test_extract_uses_stored_raw_without_refetching(client, make_user, resend, mail_extractor):
    sun = make_user("sun")
    resend.received = [_email("re_1", "sun@example.com")]
    client.post("/received/mail/re_1", headers=sun)  # by-id retrieve stores raw
    assert resend.fetched == ["re_1"]

    client.post("/received/mail/extract/re_1", headers=sun)
    # raw was already stored, so extract did not call Resend again.
    assert resend.fetched == ["re_1"]


def test_extract_unmatched_sender_is_ignored(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")
    _retrieve_list(client, sun, resend, [_email("re_2", "stranger@nowhere.example")])

    body = client.post("/received/mail/extract/re_2", headers=sun).json()
    assert body["status"] == "ignored"
    assert body["user_id"] is None
    assert mail_extractor.calls == []  # no account → never calls Claude
    assert resend.fetched == []  # ...and never fetches the body


def test_extract_not_a_booking_is_rejected(client, make_user, resend, mail_extractor):
    sun = make_user("sun")
    _retrieve_list(client, sun, resend, [_email("re_3", "sun@example.com")])
    mail_extractor.result = Extraction(
        category="not_a_booking", confidence=0.99, summary="Newsletter", items=[]
    )
    body = client.post("/received/mail/extract/re_3", headers=sun).json()
    assert body["status"] == "rejected"
    assert body["extracted"]["category"] == "not_a_booking"


def test_extract_low_confidence_is_rejected(client, make_user, resend, mail_extractor):
    sun = make_user("sun")
    _retrieve_list(client, sun, resend, [_email("re_4", "sun@example.com")])
    mail_extractor.result = Extraction(category="flight", confidence=0.2, summary="?", items=[])
    assert client.post("/received/mail/extract/re_4", headers=sun).json()["status"] == "rejected"


def test_extract_error_goes_pending_with_failure_logged(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")
    _retrieve_list(client, sun, resend, [_email("re_5", "sun@example.com")])
    mail_extractor.error = MailExtractionError("Claude extraction failed: boom")

    body = client.post("/received/mail/extract/re_5", headers=sun).json()
    assert body["status"] == "pending"
    assert len(body["failures"]) == 1
    assert body["failures"][0]["step"] == "extract"


def test_extract_batch_processes_only_the_worklist(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")
    _retrieve_list(
        client, sun, resend,
        [_email("re_1", "sun@example.com"), _email("re_2", "stranger@x.example")],
    )

    body = client.post("/received/mail/extract", headers=sun).json()
    by_id = {r["resend_email_id"]: r["status"] for r in body}
    assert by_id == {"re_1": "extracted", "re_2": "ignored"}
    assert len(mail_extractor.calls) == 1  # only the matched one hit Claude

    # Both terminal now → re-running the batch is a no-op.
    again = client.post("/received/mail/extract", headers=sun).json()
    assert again == []
    assert len(mail_extractor.calls) == 1


def test_extract_single_terminal_needs_force(client, make_user, resend, mail_extractor):
    sun = make_user("sun")
    _retrieve_list(client, sun, resend, [_email("re_1", "sun@example.com")])

    client.post("/received/mail/extract/re_1", headers=sun)  # → extracted
    assert len(mail_extractor.calls) == 1
    client.post("/received/mail/extract/re_1", headers=sun)  # terminal for extract, no re-run
    assert len(mail_extractor.calls) == 1
    client.post("/received/mail/extract/re_1?force=true", headers=sun)  # forced re-run
    assert len(mail_extractor.calls) == 2


def test_extract_unknown_id_is_404(client, make_user, resend):
    sun = make_user("sun")
    assert client.post("/received/mail/extract/nope", headers=sun).status_code == 404
