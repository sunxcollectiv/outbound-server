from sqlalchemy import func, select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.resend import ResendError

FLIGHT_EMAIL = {
    "id": "re_inbound_1",
    "from": "confirmations@airline.example",
    "to": ["trips@inbound.outbound.example"],
    "subject": "Your flight to Tokyo is booked",
    "message_id": "<abc@airline.example>",
    "created_at": "2026-10-19T12:00:00.000Z",
    "attachments": [
        {"id": "att_1", "filename": "ticket.pdf", "content_type": "application/pdf", "size": 2048}
    ],
}

HOTEL_EMAIL = {
    "id": "re_inbound_2",
    "from": "stay@hotel.example",
    "to": ["trips@inbound.outbound.example"],
    "subject": "Reservation confirmed",
    "created_at": "2026-10-18T09:30:00.000Z",
}


def _receipt_count(session) -> int:
    return session.scalar(select(func.count()).select_from(EmailReceipt))


def test_process_mail_requires_auth(client):
    assert client.post("/trips/process-mail").status_code == 401


def test_first_pull_records_receipts_and_returns_resend_data(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [FLIGHT_EMAIL, HOTEL_EMAIL]

    response = client.post("/trips/process-mail", headers=sun)
    assert response.status_code == 200, response.text
    body = response.json()

    # Resend's objects come back untouched (newest first).
    assert body["emails"] == [FLIGHT_EMAIL, HOTEL_EMAIL]

    # Every email is freshly recorded as seen.
    assert body["receipts"] == {
        "re_inbound_1": {"status": "seen", "newly_recorded": True},
        "re_inbound_2": {"status": "seen", "newly_recorded": True},
    }

    # A row per email, with the metadata snapshot filled from the list response.
    assert _receipt_count(session) == 2
    flight = session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == "re_inbound_1")
    )
    assert flight.status is EmailReceiptStatus.SEEN
    assert flight.from_address == "confirmations@airline.example"
    assert flight.subject == "Your flight to Tokyo is booked"
    assert flight.message_id == "<abc@airline.example>"
    assert flight.received_at is not None
    assert flight.processed_at is None


def test_second_pull_dedupes_without_new_rows(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [FLIGHT_EMAIL, HOTEL_EMAIL]

    client.post("/trips/process-mail", headers=sun)
    second = client.post("/trips/process-mail", headers=sun)
    assert second.status_code == 200, second.text

    # Same emails, now reported as already seen — and no duplicate rows.
    assert second.json()["receipts"] == {
        "re_inbound_1": {"status": "seen", "newly_recorded": False},
        "re_inbound_2": {"status": "seen", "newly_recorded": False},
    }
    assert _receipt_count(session) == 2


def test_new_email_among_seen_ones_is_the_only_newly_recorded(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = [FLIGHT_EMAIL]
    client.post("/trips/process-mail", headers=sun)

    # A newer email arrives alongside the already-seen one.
    resend.received = [HOTEL_EMAIL, FLIGHT_EMAIL]
    body = client.post("/trips/process-mail", headers=sun).json()

    assert body["receipts"]["re_inbound_2"]["newly_recorded"] is True
    assert body["receipts"]["re_inbound_1"]["newly_recorded"] is False
    assert _receipt_count(session) == 2


def test_empty_inbox_records_nothing(client, make_user, resend, session):
    sun = make_user("sun")
    resend.received = []

    body = client.post("/trips/process-mail", headers=sun).json()
    assert body == {"emails": [], "receipts": {}}
    assert _receipt_count(session) == 0


def test_process_mail_surfaces_upstream_failure(client, make_user, resend):
    sun = make_user("sun")
    resend.error = ResendError("Resend returned 500: boom")

    response = client.post("/trips/process-mail", headers=sun)
    assert response.status_code == 502, response.text
