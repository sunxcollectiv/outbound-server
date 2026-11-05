from sqlalchemy import select

from app.models import EmailReceipt, EmailReceiptStatus, Place
from app.services.mail_extraction import (
    ExtractedItem,
    ExtractedLodging,
    ExtractedPlace,
    ExtractedRoom,
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


def _make_extracted(client, admin, resend, mail_extractor, email_id, sender, extraction):
    """Drive a receipt to `extracted` via the retrieve + extract endpoints."""
    resend.received = [_email(email_id, sender)]
    mail_extractor.result = extraction
    assert client.post("/admin/received/mail", headers=admin).status_code == 200
    r = client.post(f"/admin/received/mail/extract/{email_id}", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "extracted", r.text


def _receipt(session, email_id):
    return session.scalar(select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id))


def test_import_requires_auth(client):
    assert client.post("/admin/received/mail/import").status_code == 401
    assert client.post("/admin/received/mail/import/re_1").status_code == 401


def test_import_places_flight_into_matching_trip(client, make_user, admin, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)  # 2026-04-09 .. 04-15, overlaps the flight
    _make_extracted(client, admin, resend, mail_extractor, "re_1", "sun@example.com", FLIGHT)

    body = client.post("/admin/received/mail/import/re_1", headers=admin).json()
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


def test_import_places_lodging_with_item_level_times(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_2", "sun@example.com", LODGING)

    trip_id = client.post("/admin/received/mail/import/re_2", headers=admin).json()["trip_id"]
    item = client.get(f"/trips/{trip_id}/items", headers=sun).json()[0]
    assert item["kind"] == "lodging"
    assert item["starts_at"] == "2026-04-10T15:00:00" and item["start_timezone"] == "Asia/Tokyo"
    assert item["confirmation_number"] == "H999"
    assert item["place"]["name"] == "Hotel Tokyo"  # resolved (created)


def _stay(start, end, lodging=None, **item_fields):
    return Extraction(
        category="lodging", confidence=0.9, summary="Stay",
        items=[ExtractedItem(
            kind="lodging", title="Kyoto Ryokan", start_local=start, end_local=end,
            start_timezone="Asia/Tokyo", end_timezone="Asia/Tokyo",
            place=ExtractedPlace(name="Kyoto Ryokan", address="1 Gion", city="Kyoto"),
            lodging=lodging, **item_fields,
        )],
    )


def test_import_lodging_stores_stay_details(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    details = ExtractedLodging(
        property_type="hotel", room_type="Deluxe Twin", guests=3, room_count=2,
        phone="+81 75-000-0000", free_cancellation_until="2026-04-08T23:59",
    )
    _make_extracted(client, admin, resend, mail_extractor, "re_l1", "sun@example.com",
                    _stay("2026-04-10T16:00", "2026-04-12T10:30", details, confirmation_number="RYO1"))

    assert client.post("/admin/received/mail/import/re_l1", headers=admin).json()["outcome"] == "placed"
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["kind"] == "lodging" and item["confirmation_number"] == "RYO1"
    # Stated times are kept as written.
    assert (item["starts_at"], item["ends_at"]) == ("2026-04-10T16:00:00", "2026-04-12T10:30:00")
    assert item["place"]["name"] == "Kyoto Ryokan" and item["end_place"] is None
    assert item["lodging"] == {
        "property_type": "hotel", "phone": "+81 75-000-0000",
        "free_cancellation_until": "2026-04-08T23:59:00",
    }
    # "2 Deluxe Twin rooms, 3 guests": two rooms of that type. A total of 3
    # can't be split between them, so the rooms carry no guest count.
    assert [(r["position"], r["room_type"], r["guests"], r["occupants"]) for r in item["rooms"]] == [
        (0, "Deluxe Twin", None, []),
        (1, "Deluxe Twin", None, []),
    ]


def test_import_date_only_lodging_gets_default_check_in_and_out(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_l2", "sun@example.com",
                    _stay("2026-04-10", "2026-04-13"))

    assert client.post("/admin/received/mail/import/re_l2", headers=admin).json()["outcome"] == "placed"
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["starts_at"] == "2026-04-10T15:00:00"  # check-in 15:00
    assert item["ends_at"] == "2026-04-13T11:00:00"  # check-out 11:00
    assert item["starts_at_utc"] == "2026-04-10T06:00:00Z"
    assert item["lodging"] is None  # nothing stated beyond the dates


def test_import_lodging_mixes_stated_and_default_times(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    # Check-in time stated, check-out only a date.
    _make_extracted(client, admin, resend, mail_extractor, "re_l3", "sun@example.com",
                    _stay("2026-04-10T14:00", "2026-04-11"))

    client.post("/admin/received/mail/import/re_l3", headers=admin)
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert (item["starts_at"], item["ends_at"]) == ("2026-04-10T14:00:00", "2026-04-11T11:00:00")


def test_import_lodging_details_are_cleaned_not_fatal(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    # An unknown type becomes `other`, a nonsense count is dropped, a blank
    # room type is no type, a date-only deadline is read as the start of that day.
    details = ExtractedLodging(property_type="capsule", guests=0, room_count=1, room_type="  ",
                               free_cancellation_until="2026-04-08")
    _make_extracted(client, admin, resend, mail_extractor, "re_l4", "sun@example.com",
                    _stay("2026-04-10", "2026-04-12", details))

    assert client.post("/admin/received/mail/import/re_l4", headers=admin).json()["outcome"] == "placed"
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["lodging"] == {
        "property_type": "other", "phone": None, "free_cancellation_until": "2026-04-08T00:00:00",
    }
    assert [(r["room_type"], r["guests"]) for r in item["rooms"]] == [(None, None)]


def test_import_a_single_room_keeps_its_type_and_guests(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_l6", "sun@example.com",
                    _stay("2026-04-10", "2026-04-12", ExtractedLodging(room_type="King", guests=2)))
    client.post("/admin/received/mail/import/re_l6", headers=admin)
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["lodging"] is None  # nothing left for the details row
    assert [(r["room_type"], r["guests"]) for r in item["rooms"]] == [("King", 2)]


def test_import_a_group_block_is_not_laid_out_room_by_room(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_l7", "sun@example.com",
                    _stay("2026-04-10", "2026-04-12", ExtractedLodging(room_count=30)))
    client.post("/admin/received/mail/import/re_l7", headers=admin)
    assert client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]["rooms"] == []


def _agent_stay(title, confirmation, start, end):
    return ExtractedItem(
        kind="lodging", title=title, confirmation_number=confirmation, booking_number="JTB-778201",
        start_local=start, end_local=end, start_timezone="Asia/Tokyo", end_timezone="Asia/Tokyo",
        place=ExtractedPlace(name=title, city="Kyoto"),
        lodging=ExtractedLodging(property_type="hotel", guests=2),
    )


# A travel agent's itinerary: three hotels under one agency booking number.
AGENT_ITINERARY = Extraction(
    category="lodging", confidence=0.95, summary="JTB Japan hotels",
    items=[
        _agent_stay("Park Hyatt Tokyo", "PH-55120", "2026-04-10", "2026-04-13"),
        _agent_stay("Tawaraya Ryokan", "TW8812", "2026-04-13T15:00", "2026-04-15T11:00"),
        _agent_stay("Hotel Kanra Kyoto", None, "2026-04-15", "2026-04-16"),
    ],
)


def test_import_agent_itinerary_splits_into_one_item_per_stay(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)  # 04-09 .. 04-15: overlaps the whole itinerary
    _make_extracted(client, admin, resend, mail_extractor, "re_l5", "sun@example.com", AGENT_ITINERARY)

    body = client.post("/admin/received/mail/import/re_l5", headers=admin).json()
    assert (body["outcome"], body["item_count"]) == ("placed", 3)
    items = client.get(f"/trips/{trip['id']}/items?kind=lodging", headers=sun).json()
    assert [
        (i["display_title"], i["confirmation_number"], i["booking_number"], i["starts_at"], i["ends_at"])
        for i in items
    ] == [
        ("Park Hyatt Tokyo", "PH-55120", "JTB-778201", "2026-04-10T15:00:00", "2026-04-13T11:00:00"),
        ("Tawaraya Ryokan", "TW8812", "JTB-778201", "2026-04-13T15:00:00", "2026-04-15T11:00:00"),
        ("Hotel Kanra Kyoto", None, "JTB-778201", "2026-04-15T15:00:00", "2026-04-16T11:00:00"),
    ]
    # Each stay is its own item at its own property.
    assert len({i["id"] for i in items}) == len({i["place"]["id"] for i in items}) == 3


def test_import_itemized_rooms_arrive_with_nobody_in_them(
    client, make_user, admin, make_trip, resend, mail_extractor
):
    sun = make_user("sun")
    trip = make_trip(sun)
    details = ExtractedLodging(property_type="hotel", room_count=2, rooms=[
        ExtractedRoom(label="Room 1", room_type="Twin", guests=2),
        ExtractedRoom(label="Room 2", room_type="King", guests=1, confirmation_number="R2"),
    ])
    _make_extracted(client, admin, resend, mail_extractor, "re_r1", "sun@example.com",
                    _stay("2026-04-10", "2026-04-12", details))

    assert client.post("/admin/received/mail/import/re_r1", headers=admin).json()["outcome"] == "placed"
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    # Itemized rooms win over the summary count.
    assert [(r["label"], r["room_type"], r["guests"], r["confirmation_number"], r["occupants"])
            for r in item["rooms"]] == [
        ("Room 1", "Twin", 2, None, []),
        ("Room 2", "King", 1, "R2", []),
    ]
    # The importer is a participant, in no room: people are placed by hand.
    assert [p["room_id"] for p in item["participants"]] == [None]


def test_import_reads_an_older_extraction_room_count(
    client, make_user, admin, make_trip, resend, mail_extractor, session
):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_r2", "sun@example.com",
                    _stay("2026-04-10", "2026-04-12", ExtractedLodging(guests=2)))
    # Stored before the rename: the count sat in "rooms".
    receipt = _receipt(session, "re_r2")
    extracted = dict(receipt.extracted)
    extracted["items"] = [{**extracted["items"][0], "lodging": {"room_type": "Twin", "rooms": 3}}]
    receipt.extracted = extracted
    session.flush()

    client.post("/admin/received/mail/import/re_r2", headers=admin)
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert [r["room_type"] for r in item["rooms"]] == ["Twin", "Twin", "Twin"]


def test_extracted_lodging_tolerates_wordy_counts():
    parsed = ExtractedLodging.model_validate({"guests": "2 adults", "rooms": "1"})
    assert (parsed.guests, parsed.room_count) == (None, 1)


def test_import_no_matching_trip_stays_extracted(client, make_user, admin, resend, mail_extractor, session):
    sun = make_user("sun")  # no trips at all
    _make_extracted(client, admin, resend, mail_extractor, "re_3", "sun@example.com", FLIGHT)

    body = client.post("/admin/received/mail/import/re_3", headers=admin).json()
    assert body["outcome"] == "no_match"
    assert _receipt(session, "re_3").status is EmailReceiptStatus.EXTRACTED


def test_import_ambiguous_when_two_trips_overlap(client, make_user, admin, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    t1, t2 = make_trip(sun), make_trip(sun)  # two trips over the same dates
    _make_extracted(client, admin, resend, mail_extractor, "re_4", "sun@example.com", FLIGHT)

    body = client.post("/admin/received/mail/import/re_4", headers=admin).json()
    assert body["outcome"] == "ambiguous"
    assert set(body["candidate_trip_ids"]) == {t1["id"], t2["id"]}
    assert _receipt(session, "re_4").status is EmailReceiptStatus.EXTRACTED


def test_import_ignores_trips_the_sender_isnt_on(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    bob = make_user("bob")
    make_trip(bob)  # covers the dates, but belongs to bob
    _make_extracted(client, admin, resend, mail_extractor, "re_5", "sun@example.com", FLIGHT)

    assert client.post("/admin/received/mail/import/re_5", headers=admin).json()["outcome"] == "no_match"


def test_reimport_is_a_noop(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_6", "sun@example.com", FLIGHT)

    assert client.post("/admin/received/mail/import/re_6", headers=admin).json()["outcome"] == "placed"
    again = client.post("/admin/received/mail/import/re_6", headers=admin).json()
    assert again["outcome"] == "skipped"  # already processed
    assert len(client.get(f"/trips/{trip['id']}/items", headers=sun).json()) == 1  # not doubled


def test_import_batch_places_only_unambiguous(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    # re_a matches uniquely; re_b has no dates in the extraction → no_match.
    _make_extracted(client, admin, resend, mail_extractor, "re_a", "sun@example.com", FLIGHT)
    dateless = Extraction(category="other", confidence=0.9, summary="?",
                          items=[ExtractedItem(kind="activity", title="Mystery")])
    _make_extracted(client, admin, resend, mail_extractor, "re_b", "sun@example.com", dateless)

    results = {r["receipt"]["resend_email_id"]: r["outcome"]
               for r in client.post("/admin/received/mail/import", headers=admin).json()}
    assert results == {"re_a": "placed", "re_b": "no_match"}


def test_import_resolves_layover_airport_and_dedupes_it(client, make_user, admin, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)
    _make_extracted(client, admin, resend, mail_extractor, "re_7", "sun@example.com", FLIGHT_2LEG)
    client.post("/admin/received/mail/import/re_7", headers=admin)

    days = client.get(f"/trips/{trip['id']}/itinerary", headers=sun).json()["days"]
    layovers = [e for day in days for e in day["items"] if e["phase"] == "layover"]
    assert len(layovers) == 1
    assert layovers[0]["layover_place"]["name"] == "HND"  # the airport now shows (was null)
    assert layovers[0]["layover_minutes"] == 180

    # The middle airport resolved to one Place: leg 1 arrival == leg 2 departure.
    legs = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]["segments"]
    assert legs[0]["end_place"]["id"] == legs[1]["place"]["id"]
    assert len(session.scalars(select(Place).where(Place.name == "HND")).all()) == 1


def test_import_reuses_place_on_name_and_address_match(client, make_user, admin, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    trip = make_trip(sun)
    existing = Place(name="Hotel Tokyo", address="1-1 Chiyoda")
    session.add(existing)
    session.commit()

    _make_extracted(client, admin, resend, mail_extractor, "re_8", "sun@example.com", LODGING_ADDR)
    client.post("/admin/received/mail/import/re_8", headers=admin)

    # Solid name + address match → reuse, not duplicate.
    assert len(session.scalars(select(Place).where(Place.name == "Hotel Tokyo")).all()) == 1
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert item["place"]["id"] == str(existing.id)


def test_import_does_not_merge_on_name_alone(client, make_user, admin, make_trip, resend, mail_extractor, session):
    sun = make_user("sun")
    make_trip(sun)
    session.add(Place(name="LAX"))  # same name, but no address to match on
    session.commit()

    _make_extracted(client, admin, resend, mail_extractor, "re_9", "sun@example.com", FLIGHT)
    client.post("/admin/received/mail/import/re_9", headers=admin)

    # Name alone isn't a solid match → a new LAX place is created, not merged.
    assert len(session.scalars(select(Place).where(Place.name == "LAX")).all()) == 2


def test_import_unknown_id_is_404(client, make_user, admin):
    sun = make_user("sun")
    assert client.post("/admin/received/mail/import/nope", headers=admin).status_code == 404


# --- process (pull a page of recent mail from Resend + extract + import) ---


def test_process_requires_auth(client):
    assert client.post("/admin/received/mail/process").status_code == 401


def test_process_pulls_and_runs_recent_mail(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    resend.received = [_email("re_1", "sun@example.com")]
    mail_extractor.result = FLIGHT

    results = client.post("/admin/received/mail/process", headers=admin).json()
    assert len(results) == 1
    assert results[0]["outcome"] == "placed"
    assert results[0]["trip_id"] == trip["id"]
    assert results[0]["receipt"]["status"] == "processed"


def test_process_limit_bounds_the_page(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    resend.received = [_email(f"re_{i}", "sun@example.com") for i in range(3)]
    mail_extractor.result = FLIGHT

    results = client.post("/admin/received/mail/process?limit=2", headers=admin).json()
    assert len(results) == 2  # only the first 2 of the page were pulled
    assert len(mail_extractor.calls) == 2


def test_process_is_idempotent(client, make_user, admin, make_trip, resend, mail_extractor):
    sun = make_user("sun")
    make_trip(sun)
    resend.received = [_email("re_x", "sun@example.com")]
    mail_extractor.result = FLIGHT

    assert client.post("/admin/received/mail/process", headers=admin).json()[0]["outcome"] == "placed"
    # Re-running pulls the same mail, but it's already processed → nothing to do.
    assert client.post("/admin/received/mail/process", headers=admin).json() == []
    assert len(mail_extractor.calls) == 1
