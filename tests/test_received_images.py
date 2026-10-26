import hashlib

from sqlalchemy import func, select

from app.models import EmailReceiptStatus, ImageReceipt
from app.services.extraction import ExtractedItem, ExtractedPlace, ExtractedSegment, Extraction
from app.services.image_extraction import ImageExtractionError

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"

# A flight overlapping JAPAN_TRIP (conftest make_trip default: 2026-04-09 .. 04-15).
FLIGHT = Extraction(
    category="flight", confidence=0.95, summary="LAX to Tokyo",
    items=[ExtractedItem(
        kind="transit", transit_mode="flight", title="Flight LAX to Tokyo",
        confirmation_number="IMG123",
        segments=[ExtractedSegment(
            transit_mode="flight", carrier="ANA", flight_number="NH105",
            start_local="2026-04-09T11:00", start_timezone="America/Los_Angeles",
            end_local="2026-04-10T15:00", end_timezone="Asia/Tokyo",
            origin=ExtractedPlace(name="LAX", iata_code="LAX", city="Los Angeles"),
            destination=ExtractedPlace(name="HND", iata_code="HND", city="Tokyo"),
        )],
    )],
)


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(ImageReceipt))


def _upload(client, headers, image_id="ticket.png", data=PNG, content_type="image/png") -> str:
    """Upload an image and return its id."""
    resp = client.post(
        "/received/images/upload",
        files={"file": (image_id, data, content_type)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _extract_image(client, headers, image_extractor, extraction, filename="ticket.png") -> str:
    """Drive an image to `extracted` via the upload + extract endpoints."""
    image_extractor.result = extraction
    image_id = _upload(client, headers, filename)
    r = client.post(f"/received/images/extract/{image_id}", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "extracted", r.text
    return image_id


def test_upload_requires_auth(client):
    assert client.post(
        "/received/images/upload", files={"file": ("x.png", PNG, "image/png")}
    ).status_code == 401


def test_upload_stores_the_image(client, make_user, session):
    sun = make_user("sun")
    resp = client.post(
        "/received/images/upload",
        files={"file": ("ticket.png", PNG, "image/png")},
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "received"
    assert body["user_id"] == sun.user_id
    assert body["content_type"] == "image/png"
    assert body["filename"] == "ticket.png"
    assert body["byte_size"] == len(PNG)
    assert body["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert body["trip_id"] is None and body["extracted"] is None
    assert "data" not in body  # the bytes aren't echoed back

    # The bytes are persisted on the row.
    row = session.scalar(select(ImageReceipt).where(ImageReceipt.id == body["id"]))
    assert row.data == PNG
    assert row.status is EmailReceiptStatus.RECEIVED


def test_upload_rejects_non_image(client, make_user, session):
    sun = make_user("sun")
    resp = client.post(
        "/received/images/upload",
        files={"file": ("notes.txt", b"just text", "text/plain")},
        headers=sun,
    )
    assert resp.status_code == 415
    assert _count(session) == 0


def test_upload_rejects_empty_file(client, make_user, session):
    sun = make_user("sun")
    resp = client.post(
        "/received/images/upload",
        files={"file": ("empty.png", b"", "image/png")},
        headers=sun,
    )
    assert resp.status_code == 422
    assert _count(session) == 0


# --- extract ---


def test_extract_endpoints_require_auth(client):
    assert client.post("/received/images/extract").status_code == 401
    assert (
        client.post("/received/images/extract/00000000-0000-0000-0000-000000000000").status_code
        == 401
    )


def test_extract_one_processes_a_booking(client, make_user, image_extractor, session):
    sun = make_user("sun")
    image_id = _upload(client, sun)

    body = client.post(f"/received/images/extract/{image_id}", headers=sun).json()
    assert body["status"] == "extracted"
    assert body["extracted"]["category"] == "flight"
    assert body["extracted_at"] is not None
    assert image_extractor.calls == [(PNG, "image/png")]  # the stored bytes were sent

    row = session.get(ImageReceipt, image_id)
    assert row.status is EmailReceiptStatus.EXTRACTED


def test_extract_not_a_booking_is_rejected(client, make_user, image_extractor):
    sun = make_user("sun")
    image_id = _upload(client, sun)
    image_extractor.result = Extraction(
        category="not_a_booking", confidence=0.99, summary="A meme", items=[]
    )
    body = client.post(f"/received/images/extract/{image_id}", headers=sun).json()
    assert body["status"] == "rejected"
    assert body["extracted"]["category"] == "not_a_booking"


def test_extract_low_confidence_is_rejected(client, make_user, image_extractor):
    sun = make_user("sun")
    image_id = _upload(client, sun)
    image_extractor.result = Extraction(category="flight", confidence=0.2, summary="?", items=[])
    body = client.post(f"/received/images/extract/{image_id}", headers=sun).json()
    assert body["status"] == "rejected"


def test_extract_error_goes_pending_with_failure_logged(client, make_user, image_extractor):
    sun = make_user("sun")
    image_id = _upload(client, sun)
    image_extractor.error = ImageExtractionError("Claude extraction failed: boom")

    body = client.post(f"/received/images/extract/{image_id}", headers=sun).json()
    assert body["status"] == "pending"
    assert len(body["failures"]) == 1
    assert body["failures"][0]["step"] == "extract"


def test_extract_batch_processes_only_the_worklist(client, make_user, image_extractor):
    sun = make_user("sun")
    _upload(client, sun, "a.png")
    _upload(client, sun, "b.png")

    body = client.post("/received/images/extract", headers=sun).json()
    assert [r["status"] for r in body] == ["extracted", "extracted"]
    assert len(image_extractor.calls) == 2

    # Both terminal now → re-running the batch is a no-op.
    again = client.post("/received/images/extract", headers=sun).json()
    assert again == []
    assert len(image_extractor.calls) == 2


def test_extract_single_terminal_needs_force(client, make_user, image_extractor):
    sun = make_user("sun")
    image_id = _upload(client, sun)

    client.post(f"/received/images/extract/{image_id}", headers=sun)  # → extracted
    assert len(image_extractor.calls) == 1
    client.post(f"/received/images/extract/{image_id}", headers=sun)  # terminal, no re-run
    assert len(image_extractor.calls) == 1
    client.post(f"/received/images/extract/{image_id}?force=true", headers=sun)  # forced
    assert len(image_extractor.calls) == 2


def test_extract_unknown_id_is_404(client, make_user):
    sun = make_user("sun")
    assert (
        client.post("/received/images/extract/00000000-0000-0000-0000-000000000000", headers=sun)
        .status_code
        == 404
    )


# --- import ---

NIL_UUID = "00000000-0000-0000-0000-000000000000"


def test_import_endpoints_require_auth(client):
    assert client.post("/received/images/import").status_code == 401
    assert client.post(f"/received/images/import/{NIL_UUID}").status_code == 401


def test_import_places_flight_into_matching_trip(
    client, make_user, make_trip, image_extractor, session
):
    sun = make_user("sun")
    trip = make_trip(sun)  # 2026-04-09 .. 04-15, overlaps the flight
    image_id = _extract_image(client, sun, image_extractor, FLIGHT)

    body = client.post(f"/received/images/import/{image_id}", headers=sun).json()
    assert body["outcome"] == "placed"
    assert body["trip_id"] == trip["id"]
    assert body["item_count"] == 1
    assert body["receipt"]["status"] == "processed"
    assert body["receipt"]["trip_id"] == trip["id"]

    items = client.get(f"/trips/{trip['id']}/items", headers=sun).json()
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "transit" and item["transit_mode"] == "flight"
    assert item["confirmation_number"] == "IMG123"
    assert item["place"]["name"] == "LAX" and item["end_place"]["name"] == "HND"

    row = session.get(ImageReceipt, image_id)
    assert row.status is EmailReceiptStatus.PROCESSED and row.trip_id is not None


def test_import_no_matching_trip_stays_extracted(client, make_user, image_extractor, session):
    sun = make_user("sun")  # no trips at all
    image_id = _extract_image(client, sun, image_extractor, FLIGHT)

    body = client.post(f"/received/images/import/{image_id}", headers=sun).json()
    assert body["outcome"] == "no_match"
    assert session.get(ImageReceipt, image_id).status is EmailReceiptStatus.EXTRACTED


def test_reimport_is_a_noop(client, make_user, make_trip, image_extractor):
    sun = make_user("sun")
    make_trip(sun)
    image_id = _extract_image(client, sun, image_extractor, FLIGHT)

    assert client.post(f"/received/images/import/{image_id}", headers=sun).json()["outcome"] == "placed"
    again = client.post(f"/received/images/import/{image_id}", headers=sun).json()
    assert again["outcome"] == "skipped"  # already processed, not re-created


def test_import_batch_places_only_extracted(client, make_user, make_trip, image_extractor):
    sun = make_user("sun")
    make_trip(sun)
    _extract_image(client, sun, image_extractor, FLIGHT, "a.png")
    # An uploaded-but-not-extracted image is skipped by the import backlog.
    _upload(client, sun, "b.png")

    outcomes = [r["outcome"] for r in client.post("/received/images/import", headers=sun).json()]
    assert outcomes == ["placed"]  # only the single `extracted` image was imported


def test_import_unknown_id_is_404(client, make_user):
    sun = make_user("sun")
    assert client.post(f"/received/images/import/{NIL_UUID}", headers=sun).status_code == 404


# --- process (upload + extract + import in one call) ---


def test_process_requires_auth(client):
    assert client.post(
        "/received/images/process", files={"file": ("x.png", PNG, "image/png")}
    ).status_code == 401


def test_process_uploads_extracts_and_imports(client, make_user, make_trip, image_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    image_extractor.result = FLIGHT
    resp = client.post(
        "/received/images/process",
        files={"file": ("ticket.png", PNG, "image/png")},
        headers=sun,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outcome"] == "placed"
    assert body["trip_id"] == trip["id"]
    assert body["item_count"] == 1
    assert body["receipt"]["status"] == "processed"
    assert len(image_extractor.calls) == 1  # one upload → one vision call
    assert len(client.get(f"/trips/{trip['id']}/items", headers=sun).json()) == 1


def test_process_not_a_booking_never_imports(client, make_user, make_trip, image_extractor):
    sun = make_user("sun")
    make_trip(sun)
    image_extractor.result = Extraction(
        category="not_a_booking", confidence=0.99, summary="A meme", items=[]
    )
    body = client.post(
        "/received/images/process",
        files={"file": ("x.png", PNG, "image/png")},
        headers=sun,
    ).json()
    assert body["receipt"]["status"] == "rejected"
    assert body["outcome"] == "skipped"  # extract rejected it; import never ran
