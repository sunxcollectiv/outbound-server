from sqlalchemy import select

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_extraction import (
    ExtractedItem,
    ExtractedPlace,
    ExtractedSegment,
    Extraction,
)

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
LODGING = Extraction(
    category="lodging", confidence=0.9, summary="Hotel stay",
    items=[ExtractedItem(
        kind="lodging", title="Hotel Tokyo", confirmation_number="H999",
        start_local="2026-04-10T15:00", start_timezone="Asia/Tokyo",
        end_local="2026-04-13T10:00", end_timezone="Asia/Tokyo",
        place=ExtractedPlace(name="Hotel Tokyo", city="Tokyo"),
    )],
)


def _email(email_id: str, sender: str) -> dict:
    return {
        "id": email_id, "from": sender, "subject": "Booking",
        "message_id": f"<{email_id}@x.example>", "created_at": "2026-03-01T12:00:00.000Z",
        "text": "booking details", "html": "<p>booking</p>",
    }


def _make_extracted(client, headers, resend, mail_extractor, email_id, sender, extraction):
    """Drive a receipt to `extracted` via the retrieve + extract endpoints."""
    resend.received = [_email(email_id, sender)]
    mail_extractor.result = extraction
    assert client.post("/received/mail", headers=headers).status_code == 200
    r = client.post(f"/received/mail/extract/{email_id}", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "extracted", r.text


def _receipt(session, email_id):
    return session.scalar(select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id))


def test_import_requires_auth(client):
    assert client.post("/received/mail/import").status_code == 401
    assert client.post("/received/mail/import/re_1").status_code == 401


def test_import_places_flight_into_matching_trip(client, make_user, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)  # 2026-04-09 .. 04-15, overlaps the flight
    _make_extracted(client, sun, resend, mail_extractor, "re_1", "sun@example.com", FLIGHT)

    body = client.post("/received/mail/import/re_1", headers=sun).json()
    assert body["outcome"] == "placed"
    assert body["trip_id"] == trip["id"]
    assert body["item_count"] == 1
    assert body["receipt"]["status"] == "processed"
    assert body["receipt"]["trip_id"] == trip["id"]

    items = client.get(f"/trips/{trip['id']}/items", headers=sun).json()
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "transit"
    assert item["transit_mode"] == "flight"
    assert item["confirmation_number"] == "ABC123"
    assert item["place"] is None and item["end_place"] is None  # places deferred
    assert "LAX" in item["notes"] and "HND" in item["notes"]  # ...kept in notes
    leg = item["segments"][0]
    assert (leg["carrier"], leg["flight_number"], leg["transit_mode"]) == ("ANA", "NH105", "flight")


def test_import_places_lodging_with_item_level_times(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    _make_extracted(client, sun, resend, mail_extractor, "re_2", "sun@example.com", LODGING)

    trip_id = client.post("/received/mail/import/re_2", headers=sun).json()["trip_id"]
    item = client.get(f"/trips/{trip_id}/items", headers=sun).json()[0]
    assert item["kind"] == "lodging"
    assert item["starts_at"] == "2026-04-10T15:00:00" and item["start_timezone"] == "Asia/Tokyo"
    assert item["confirmation_number"] == "H999"
    assert item["place"] is None
    assert "Hotel Tokyo" in item["notes"]


def test_import_no_matching_trip_stays_extracted(client, make_user, resend, mail_extractor, session):
    sun = make_user("sun")  # no trips at all
    _make_extracted(client, sun, resend, mail_extractor, "re_3", "sun@example.com", FLIGHT)

    body = client.post("/received/mail/import/re_3", headers=sun).json()
    assert body["outcome"] == "no_match"
    assert _receipt(session, "re_3").status is EmailReceiptStatus.EXTRACTED


def test_import_ambiguous_when_two_trips_overlap(client, make_user, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    t1, t2 = make_trip(sun), make_trip(sun)  # two trips over the same dates
    _make_extracted(client, sun, resend, mail_extractor, "re_4", "sun@example.com", FLIGHT)

    body = client.post("/received/mail/import/re_4", headers=sun).json()
    assert body["outcome"] == "ambiguous"
    assert set(body["candidate_trip_ids"]) == {t1["id"], t2["id"]}
    assert _receipt(session, "re_4").status is EmailReceiptStatus.EXTRACTED


def test_import_ignores_trips_the_sender_isnt_on(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    bob = make_user("bob")
    make_trip(bob)  # covers the dates, but belongs to bob
    _make_extracted(client, sun, resend, mail_extractor, "re_5", "sun@example.com", FLIGHT)

    assert client.post("/received/mail/import/re_5", headers=sun).json()["outcome"] == "no_match"


def test_reimport_is_a_noop(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, sun, resend, mail_extractor, "re_6", "sun@example.com", FLIGHT)

    assert client.post("/received/mail/import/re_6", headers=sun).json()["outcome"] == "placed"
    again = client.post("/received/mail/import/re_6", headers=sun).json()
    assert again["outcome"] == "skipped"  # already processed
    assert len(client.get(f"/trips/{trip['id']}/items", headers=sun).json()) == 1  # not doubled


def test_import_batch_places_only_unambiguous(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    # re_a matches uniquely; re_b has no dates in the extraction → no_match.
    _make_extracted(client, sun, resend, mail_extractor, "re_a", "sun@example.com", FLIGHT)
    dateless = Extraction(category="other", confidence=0.9, summary="?",
                          items=[ExtractedItem(kind="activity", title="Mystery")])
    _make_extracted(client, sun, resend, mail_extractor, "re_b", "sun@example.com", dateless)

    results = {r["receipt"]["resend_email_id"]: r["outcome"]
               for r in client.post("/received/mail/import", headers=sun).json()}
    assert results == {"re_a": "placed", "re_b": "no_match"}


def test_import_unknown_id_is_404(client, make_user):
    sun = make_user("sun")
    assert client.post("/received/mail/import/nope", headers=sun).status_code == 404
