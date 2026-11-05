"""Where a trip item came from (manual / email / image, with a link to its
receipt), when a person last changed its booking, and its original."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailReceipt, ImageReceipt
from app.services.extraction import ExtractedItem, ExtractedPlace, Extraction

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"

STAY = Extraction(
    category="lodging", confidence=0.9, summary="Hotel",
    items=[ExtractedItem(
        kind="lodging", title="Hotel Tokyo", confirmation_number="H1",
        start_local="2026-04-10", end_local="2026-04-12",
        start_timezone="Asia/Tokyo", end_timezone="Asia/Tokyo",
        place=ExtractedPlace(name="Hotel Tokyo", city="Tokyo"),
    )],
)


@pytest.fixture
def trip(client, make_user, make_trip):
    """Sun organizes; Sean is on the trip too."""
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, members=[{"email": sean.email}])
    ids = {m["email"].split("@")[0]: m["id"] for m in trip["members"]}
    return {"url": f"/trips/{trip['id']}/items", "sun": sun, "sean": sean, "ids": ids}


def _upload_stay(client, headers, image_extractor):
    image_extractor.result = STAY
    response = client.post(
        "/received/images/process", files={"file": ("stay.png", PNG, "image/png")}, headers=headers
    )
    assert response.status_code == 201 and response.json()["outcome"] == "placed", response.text


def _email_stay(client, admin, resend, mail_extractor, html="<p>Your stay</p>", text="Your stay"):
    resend.received = [{
        "id": "re_1", "from": "sun@example.com", "subject": "Booking",
        "message_id": "<re_1@x>", "created_at": "2026-03-01T12:00:00.000Z",
        "text": text, "html": html,
    }]
    mail_extractor.result = STAY
    assert client.post("/admin/received/mail/re_1", headers=admin).status_code == 200
    body = client.post("/admin/received/mail/process", headers=admin).json()
    assert body[0]["outcome"] == "placed", body


def test_a_hand_entered_item_is_manual(client, trip):
    item = client.post(
        trip["url"], json={"kind": "activity", "title": "Dinner", "starts_at": "2026-04-10T19:00:00"},
        headers=trip["sun"],
    ).json()
    assert (item["source"], item["has_source"], item["edited_at"], item["edited_by"]) == (
        "manual", False, None, None
    )
    assert client.get(f"{trip['url']}/{item['id']}/source", headers=trip["sun"]).status_code == 404


def test_an_imported_screenshot_links_its_original(client, trip, image_extractor):
    _upload_stay(client, trip["sun"], image_extractor)
    [item] = client.get(trip["url"], headers=trip["sun"]).json()
    assert (item["source"], item["has_source"], item["edited_at"]) == ("image", True, None)

    original = client.get(f"{trip['url']}/{item['id']}/source", headers=trip["sun"])
    assert original.status_code == 200
    assert original.content == PNG
    assert original.headers["content-type"] == "image/png"
    assert original.headers["cache-control"] == "private, no-store"


def test_an_imported_email_serves_its_html(client, trip, admin, resend, mail_extractor, session):
    _email_stay(client, admin, resend, mail_extractor)
    [item] = client.get(trip["url"], headers=trip["sun"]).json()
    assert (item["source"], item["has_source"]) == ("email", True)

    original = client.get(f"{trip['url']}/{item['id']}/source", headers=trip["sun"])
    assert original.status_code == 200 and original.text == "<p>Your stay</p>"
    assert original.headers["content-type"].startswith("text/html")
    assert original.headers["content-security-policy"].startswith("sandbox")

    # Deleting the receipt keeps the item, with no original left to show.
    session.delete(session.scalar(select(EmailReceipt)))
    session.flush()
    session.expire_all()
    [kept] = client.get(trip["url"], headers=trip["sun"]).json()
    assert (kept["source"], kept["has_source"]) == ("email", False)
    assert client.get(f"{trip['url']}/{item['id']}/source", headers=trip["sun"]).status_code == 404


def test_a_text_only_email_serves_text(client, trip, admin, resend, mail_extractor):
    _email_stay(client, admin, resend, mail_extractor, html=None, text="Your stay, in plain text")
    [item] = client.get(trip["url"], headers=trip["sun"]).json()
    original = client.get(f"{trip['url']}/{item['id']}/source", headers=trip["sun"])
    assert original.text == "Your stay, in plain text"
    assert original.headers["content-type"].startswith("text/plain")


def test_only_participants_see_the_original(client, trip, image_extractor, monkeypatch):
    url, sun, sean, ids = trip["url"], trip["sun"], trip["sean"], trip["ids"]
    _upload_stay(client, sun, image_extractor)
    [item] = client.get(url, headers=sun).json()
    source = f"{url}/{item['id']}/source"

    # Sean can see the (open) item, but isn't on it — and is turned away
    # before the image (possibly megabytes) is even loaded.
    loaded = []
    real_get = Session.get

    def recording_get(self, entity, ident, **kwargs):
        loaded.append(entity)
        return real_get(self, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", recording_get)
    assert client.get(f"{url}/{item['id']}", headers=sean).status_code == 200
    assert client.get(source, headers=sean).status_code == 403
    assert ImageReceipt not in loaded
    monkeypatch.undo()

    # Once he's a participant, he can.
    client.patch(
        f"{url}/{item['id']}",
        json={"participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}]},
        headers=sun,
    )
    assert client.get(source, headers=sean).status_code == 200


def test_closed_items_hide_their_provenance_from_outsiders(client, trip, image_extractor):
    url, sun, sean = trip["url"], trip["sun"], trip["sean"]
    _upload_stay(client, sun, image_extractor)
    [item] = client.get(url, headers=sun).json()
    client.patch(f"{url}/{item['id']}", json={"visibility": "closed", "title": "Hotel"}, headers=sun)
    [seen] = client.get(url, headers=sean).json()
    assert seen["redacted"] is True
    assert (seen["has_source"], seen["edited_by"]) == (False, None)
    assert client.get(f"{url}/{item['id']}/source", headers=sean).status_code == 403


def test_editing_the_booking_stamps_who_and_when(client, trip, image_extractor, clock):
    url, sun, sean, ids = trip["url"], trip["sun"], trip["sean"], trip["ids"]
    _upload_stay(client, sun, image_extractor)
    [item] = client.get(url, headers=sun).json()
    item_url = f"{url}/{item['id']}"

    def patch(body, headers=sun):
        response = client.patch(item_url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    # The user's own arrangement on top of the booking isn't editing it.
    for body in (
        {"participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}]},
        {"visibility": "private"},
        {"notes": "Ask for a high floor"},
        {"tags": ["work"]},
        {"requirement": "optional"},
        {"title": "Hotel Tokyo"},  # unchanged
    ):
        assert patch(body)["edited_at"] is None, body

    # Rooms are the booking; who's in them isn't.
    with_room = patch({"rooms": [{"label": "Room 1"}]})
    assert with_room["edited_at"] is not None
    first_edit = with_room["edited_at"]
    room_id = with_room["rooms"][0]["id"]
    clock.now = clock.now.replace(hour=13)
    moved = patch({"rooms": [{"id": room_id, "label": "Room 1", "occupants": [ids["sean"]]}]})
    assert moved["edited_at"] == first_edit

    # A real change, by Sean this time.
    changed = patch({"starts_at": "2026-04-10T16:00:00"}, headers=sean)
    assert changed["edited_at"] == "2026-11-03T13:00:00Z"
    assert changed["edited_by"]["email"] == "sean@example.com"
    # Still an import, with its original.
    assert (changed["source"], changed["has_source"]) == ("image", True)


@pytest.mark.parametrize(
    "body",
    [
        {"confirmation_number": "H2"},
        {"booking_number": "AGENT-1"},
        {"lodging": {"phone": "+81 3"}},
        {"kind": "activity"},
        {"ends_at": "2026-04-13T11:00:00"},
    ],
)
def test_each_booking_field_counts_as_an_edit(client, trip, image_extractor, body):
    _upload_stay(client, trip["sun"], image_extractor)
    [item] = client.get(trip["url"], headers=trip["sun"]).json()
    edited = client.patch(f"{trip['url']}/{item['id']}", json=body, headers=trip["sun"])
    assert edited.status_code == 200, edited.text
    assert edited.json()["edited_at"] is not None
