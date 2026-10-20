from app.services.resend import ResendError

FLIGHT_EMAIL = {
    "id": "re_inbound_1",
    "from": "confirmations@airline.example",
    "to": ["trips@inbound.outbound.example"],
    "subject": "Your flight to Tokyo is booked",
    "created_at": "2026-10-19T12:00:00.000Z",
    "attachments": [
        {"id": "att_1", "filename": "ticket.pdf", "content_type": "application/pdf", "size": 2048}
    ],
}


def test_process_mail_requires_auth(client):
    assert client.post("/trips/process-mail").status_code == 401


def test_process_mail_returns_resend_response_as_is(client, make_user, resend):
    sun = make_user("sun")
    resend.received = [FLIGHT_EMAIL]

    response = client.post("/trips/process-mail", headers=sun)
    assert response.status_code == 200, response.text
    # Exactly what Resend handed back, untouched.
    assert response.json() == {
        "object": "list",
        "has_more": False,
        "data": [FLIGHT_EMAIL],
    }


def test_process_mail_surfaces_upstream_failure(client, make_user, resend):
    sun = make_user("sun")
    resend.error = ResendError("Resend returned 500: boom")

    response = client.post("/trips/process-mail", headers=sun)
    assert response.status_code == 502, response.text
