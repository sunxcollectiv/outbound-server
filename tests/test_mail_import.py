from sqlalchemy import select

from app.models import EmailReceipt, EmailReceiptStatus, Place
from app.services.mail_extraction import (
    ExtractedItem,
    ExtractedPlace,
    ExtractedSegment,
    Extraction,
)


def _leg(carrier, number, s, stz, e, etz, origin, dest):
    return ExtractedSegment(
        transit_mode="flight", carrier=carrier, flight_number=number,
        start_local=s, start_timezone=stz, end_local=e, end_timezone=etz,
        origin=ExtractedPlace(name=origin, iata_code=origin, city=origin),
        destination=ExtractedPlace(name=dest, iata_code=dest, city=dest),
    )


# A two-leg flight LAX -> HND -> ITM, with a layover at HND (15:00 -> 18:00 = 3h).
FLIGHT_2LEG = Extraction(
    category="flight", confidence=0.95, summary="LAX to Osaka via Tokyo",
    items=[ExtractedItem(
        kind="transit", transit_mode="flight", title="LAX to Osaka via Tokyo",
        confirmation_number="MULTI1",
        segments=[
            _leg("ANA", "NH1", "2026-04-09T11:00", "America/Los_Angeles",
                 "2026-04-10T15:00", "Asia/Tokyo", "LAX", "HND"),
            _leg("ANA", "NH21", "2026-04-10T18:00", "Asia/Tokyo",
                 "2026-04-10T19:30", "Asia/Tokyo", "HND", "ITM"),
        ],
    )],
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
def _lodging(confirmation, **place_fields):
    return Extraction(
        category="lodging", confidence=0.9, summary="Hotel stay",
        items=[ExtractedItem(
            kind="lodging", title="Hotel Tokyo", confirmation_number=confirmation,
            start_local="2026-04-10T15:00", start_timezone="Asia/Tokyo",
            end_local="2026-04-13T10:00", end_timezone="Asia/Tokyo",
            place=ExtractedPlace(name="Hotel Tokyo", city="Tokyo", **place_fields),
        )],
    )


LODGING = _lodging("H999")  # no street address
LODGING_ADDR = _lodging("H111", address="1-1 Chiyoda")  # with address


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
    # Places resolved (match-or-create), rolled up onto the item from its legs.
    assert item["place"]["name"] == "LAX" and item["end_place"]["name"] == "HND"
    leg = item["segments"][0]
    assert (leg["carrier"], leg["flight_number"], leg["transit_mode"]) == ("ANA", "NH105", "flight")
    assert leg["place"]["name"] == "LAX" and leg["end_place"]["name"] == "HND"
    # A created place has no Google id (places no longer require one).
    lax = session.scalar(select(Place).where(Place.name == "LAX"))
    assert lax.google_place_id is None


def test_import_places_lodging_with_item_level_times(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    _make_extracted(client, sun, resend, mail_extractor, "re_2", "sun@example.com", LODGING)

    trip_id = client.post("/received/mail/import/re_2", headers=sun).json()["trip_id"]
    item = client.get(f"/trips/{trip_id}/items", headers=sun).json()[0]
    assert item["kind"] == "lodging"
    assert item["starts_at"] == "2026-04-10T15:00:00" and item["start_timezone"] == "Asia/Tokyo"
    assert item["confirmation_number"] == "H999"
    assert item["place"]["name"] == "Hotel Tokyo"  # resolved (created)


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


def test_import_resolves_layover_airport_and_dedupes_it(client, make_user, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, sun, resend, mail_extractor, "re_7", "sun@example.com", FLIGHT_2LEG)
    client.post("/received/mail/import/re_7", headers=sun)

    days = client.get(f"/trips/{trip['id']}/itinerary", headers=sun).json()["days"]
    layovers = [e for day in days for e in day["items"] if e["phase"] == "layover"]
    assert len(layovers) == 1
    assert layovers[0]["layover_place"]["name"] == "HND"  # the airport now shows (was null)
    assert layovers[0]["layover_minutes"] == 180

    # The middle airport resolved to one Place: leg 1 arrival == leg 2 departure.
    legs = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]["segments"]
    assert legs[0]["end_place"]["id"] == legs[1]["place"]["id"]
    assert len(session.scalars(select(Place).where(Place.name == "HND")).all()) == 1


def test_import_reuses_place_on_name_and_address_match(client, make_user, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)
    existing = Place(name="Hotel Tokyo", address="1-1 Chiyoda")
    session.add(existing)
    session.commit()

    _make_extracted(client, sun, resend, mail_extractor, "re_8", "sun@example.com", LODGING_ADDR)
    client.post("/received/mail/import/re_8", headers=sun)

    # Solid name + address match → reuse, not duplicate.
    assert len(session.scalars(select(Place).where(Place.name == "Hotel Tokyo")).all()) == 1
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["place"]["id"] == str(existing.id)


def test_import_does_not_merge_on_name_alone(client, make_user, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    make_trip(sun)
    session.add(Place(name="LAX"))  # same name, but no address to match on
    session.commit()

    _make_extracted(client, sun, resend, mail_extractor, "re_9", "sun@example.com", FLIGHT)
    client.post("/received/mail/import/re_9", headers=sun)

    # Name alone isn't a solid match → a new LAX place is created, not merged.
    assert len(session.scalars(select(Place).where(Place.name == "LAX")).all()) == 2


def test_import_unknown_id_is_404(client, make_user):
    sun = make_user("sun")
    assert client.post("/received/mail/import/nope", headers=sun).status_code == 404


# --- process (pull a page of recent mail from Resend + extract + import) ---


def test_process_requires_auth(client):
    assert client.post("/received/mail/process").status_code == 401


def test_process_pulls_and_runs_recent_mail(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    resend.received = [_email("re_1", "sun@example.com")]
    mail_extractor.result = FLIGHT

    results = client.post("/received/mail/process", headers=sun).json()
    assert len(results) == 1
    assert results[0]["outcome"] == "placed"
    assert results[0]["trip_id"] == trip["id"]
    assert results[0]["receipt"]["status"] == "processed"


def test_process_limit_bounds_the_page(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    resend.received = [_email(f"re_{i}", "sun@example.com") for i in range(3)]
    mail_extractor.result = FLIGHT

    results = client.post("/received/mail/process?limit=2", headers=sun).json()
    assert len(results) == 2  # only the first 2 of the page were pulled
    assert len(mail_extractor.calls) == 2


def test_process_is_idempotent(client, make_user, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    resend.received = [_email("re_x", "sun@example.com")]
    mail_extractor.result = FLIGHT

    assert client.post("/received/mail/process", headers=sun).json()[0]["outcome"] == "placed"
    # Re-running pulls the same mail, but it's already processed → nothing to do.
    assert client.post("/received/mail/process", headers=sun).json() == []
    assert len(mail_extractor.calls) == 1
