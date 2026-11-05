import hashlib
import uuid

from sqlalchemy import func, select

from app.models import EmailReceiptStatus, ImageReceipt
from app.services.extraction import (
    ExtractedItem,
    ExtractedLodging,
    ExtractedPlace,
    ExtractedSegment,
    Extraction,
)
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


# The step-by-step endpoints are admin (service-token) dev tooling, so an admin
# upload has no user behind it; the one-shot /received/images/process is the
# real user flow and owns its receipt.


def _upload(client, admin, image_id="ticket.png", data=PNG, content_type="image/png") -> str:
    """Upload an image via the admin endpoint and return its id (no uploader)."""
    resp = client.post(
        "/admin/received/images/upload",
        files={"file": (image_id, data, content_type)},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _extract_image(client, admin, image_extractor, extraction, filename="ticket.png") -> str:
    """Drive an image to `extracted` via the admin upload + extract endpoints."""
    image_extractor.result = extraction
    image_id = _upload(client, admin, filename)
    r = client.post(f"/admin/received/images/extract/{image_id}", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "extracted", r.text
    return image_id


def _extracted_for(client, admin, session, image_extractor, extraction, user) -> str:
    """An `extracted` image attributed to `user` — upload+extract via admin (no
    uploader), then point the receipt at the user so admin import can match
    their trips. Stands in for a receipt a user owns."""
    image_id = _extract_image(client, admin, image_extractor, extraction)
    session.get(ImageReceipt, uuid.UUID(image_id)).user_id = uuid.UUID(user.user_id)
    session.flush()
    return image_id


# --- upload (admin) ---


def test_upload_requires_auth(client):
    assert client.post(
        "/admin/received/images/upload", files={"file": ("x.png", PNG, "image/png")}
    ).status_code == 401


def test_received_images_admin_ops_are_admin_only(client, make_user):
    """The step-by-step endpoints are admin-only; a normal user is forbidden."""
    sun = make_user("sun")
    upload = client.post(
        "/admin/received/images/upload",
        files={"file": ("x.png", PNG, "image/png")},
        headers=sun,
    )
    assert upload.status_code == 403
    assert client.post("/admin/received/images/extract", headers=sun).status_code == 403
    assert client.post("/admin/received/images/import", headers=sun).status_code == 403


def test_upload_stores_the_image(client, admin, session):
    resp = client.post(
        "/admin/received/images/upload",
        files={"file": ("ticket.png", PNG, "image/png")},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "received"
    assert body["user_id"] is None  # an admin upload has no uploader
    assert body["content_type"] == "image/png"
    assert body["filename"] == "ticket.png"
    assert body["byte_size"] == len(PNG)
    assert body["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert body["trip_id"] is None and body["extracted"] is None
    assert "data" not in body  # the bytes aren't echoed back

    row = session.scalar(select(ImageReceipt).where(ImageReceipt.id == body["id"]))
    assert row.data == PNG
    assert row.status is EmailReceiptStatus.RECEIVED


def test_upload_rejects_non_image(client, admin, session):
    resp = client.post(
        "/admin/received/images/upload",
        files={"file": ("notes.txt", b"just text", "text/plain")},
        headers=admin,
    )
    assert resp.status_code == 415
    assert _count(session) == 0


def test_upload_rejects_empty_file(client, admin, session):
    resp = client.post(
        "/admin/received/images/upload",
        files={"file": ("empty.png", b"", "image/png")},
        headers=admin,
    )
    assert resp.status_code == 422
    assert _count(session) == 0


# --- extract (admin) ---


def test_extract_endpoints_require_auth(client):
    assert client.post("/admin/received/images/extract").status_code == 401
    assert (
        client.post("/admin/received/images/extract/00000000-0000-0000-0000-000000000000").status_code
        == 401
    )


def test_extract_one_processes_a_booking(client, admin, image_extractor, session):
    image_id = _upload(client, admin)

    body = client.post(f"/admin/received/images/extract/{image_id}", headers=admin).json()
    assert body["status"] == "extracted"
    assert body["extracted"]["category"] == "flight"
    assert body["extracted_at"] is not None
    assert image_extractor.calls == [(PNG, "image/png")]  # the stored bytes were sent

    row = session.get(ImageReceipt, uuid.UUID(image_id))
    assert row.status is EmailReceiptStatus.EXTRACTED


def test_extract_not_a_booking_is_rejected(client, admin, image_extractor):
    image_id = _upload(client, admin)
    image_extractor.result = Extraction(
        category="not_a_booking", confidence=0.99, summary="A meme", items=[]
    )
    body = client.post(f"/admin/received/images/extract/{image_id}", headers=admin).json()
    assert body["status"] == "rejected"
    assert body["extracted"]["category"] == "not_a_booking"


def test_extract_low_confidence_is_rejected(client, admin, image_extractor):
    image_id = _upload(client, admin)
    image_extractor.result = Extraction(category="flight", confidence=0.2, summary="?", items=[])
    body = client.post(f"/admin/received/images/extract/{image_id}", headers=admin).json()
    assert body["status"] == "rejected"


def test_extract_error_goes_pending_with_failure_logged(client, admin, image_extractor):
    image_id = _upload(client, admin)
    image_extractor.error = ImageExtractionError("Claude extraction failed: boom")

    body = client.post(f"/admin/received/images/extract/{image_id}", headers=admin).json()
    assert body["status"] == "pending"
    assert len(body["failures"]) == 1
    assert body["failures"][0]["step"] == "extract"


def test_extract_batch_processes_only_the_worklist(client, admin, image_extractor):
    _upload(client, admin, "a.png")
    _upload(client, admin, "b.png")

    body = client.post("/admin/received/images/extract", headers=admin).json()
    assert [r["status"] for r in body] == ["extracted", "extracted"]
    assert len(image_extractor.calls) == 2

    # Both terminal now → re-running the batch is a no-op.
    again = client.post("/admin/received/images/extract", headers=admin).json()
    assert again == []
    assert len(image_extractor.calls) == 2


def test_extract_single_terminal_needs_force(client, admin, image_extractor):
    image_id = _upload(client, admin)

    client.post(f"/admin/received/images/extract/{image_id}", headers=admin)  # → extracted
    assert len(image_extractor.calls) == 1
    client.post(f"/admin/received/images/extract/{image_id}", headers=admin)  # terminal, no re-run
    assert len(image_extractor.calls) == 1
    client.post(f"/admin/received/images/extract/{image_id}?force=true", headers=admin)  # forced
    assert len(image_extractor.calls) == 2


def test_extract_unknown_id_is_404(client, admin):
    assert (
        client.post("/admin/received/images/extract/00000000-0000-0000-0000-000000000000", headers=admin)
        .status_code
        == 404
    )


# --- import (admin) ---

NIL_UUID = "00000000-0000-0000-0000-000000000000"


def test_import_endpoints_require_auth(client):
    assert client.post("/admin/received/images/import").status_code == 401
    assert client.post(f"/admin/received/images/import/{NIL_UUID}").status_code == 401


def test_import_places_flight_into_matching_trip(
    client, make_user, admin, make_trip, image_extractor, session
):
    sun = make_user("sun")
    trip = make_trip(sun)  # 2026-04-09 .. 04-15, overlaps the flight
    image_id = _extracted_for(client, admin, session, image_extractor, FLIGHT, sun)

    body = client.post(f"/admin/received/images/import/{image_id}", headers=admin).json()
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

    row = session.get(ImageReceipt, uuid.UUID(image_id))
    assert row.status is EmailReceiptStatus.PROCESSED and row.trip_id is not None


def test_import_no_owner_or_trip_stays_extracted(client, admin, image_extractor, session):
    # An admin upload has no user, so there are no trips to match.
    image_id = _extract_image(client, admin, image_extractor, FLIGHT)

    body = client.post(f"/admin/received/images/import/{image_id}", headers=admin).json()
    assert body["outcome"] == "no_match"
    assert session.get(ImageReceipt, uuid.UUID(image_id)).status is EmailReceiptStatus.EXTRACTED


def test_reimport_is_a_noop(client, make_user, admin, make_trip, image_extractor, session):
    sun = make_user("sun")
    make_trip(sun)
    image_id = _extracted_for(client, admin, session, image_extractor, FLIGHT, sun)

    assert client.post(f"/admin/received/images/import/{image_id}", headers=admin).json()["outcome"] == "placed"
    again = client.post(f"/admin/received/images/import/{image_id}", headers=admin).json()
    assert again["outcome"] == "skipped"  # already processed, not re-created


def test_import_batch_places_only_extracted(client, make_user, admin, make_trip, image_extractor, session):
    sun = make_user("sun")
    make_trip(sun)
    _extracted_for(client, admin, session, image_extractor, FLIGHT, sun)
    # An uploaded-but-not-extracted image is skipped by the import backlog.
    _upload(client, admin, "b.png")

    outcomes = [r["outcome"] for r in client.post("/admin/received/images/import", headers=admin).json()]
    assert outcomes == ["placed"]  # only the single `extracted` image was imported


def test_import_unknown_id_is_404(client, admin):
    assert client.post(f"/admin/received/images/import/{NIL_UUID}", headers=admin).status_code == 404


# --- process (user-facing: upload + extract + import in one call) ---


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


def test_process_imports_a_lodging_screenshot(client, make_user, make_trip, image_extractor):
    sun = make_user("sun")
    trip = make_trip(sun)
    image_extractor.result = Extraction(
        category="lodging", confidence=0.9, summary="Airbnb in Tokyo",
        items=[ExtractedItem(
            kind="lodging", title="Shibuya loft", start_local="2026-04-11",
            end_local="2026-04-14", start_timezone="Asia/Tokyo", end_timezone="Asia/Tokyo",
            place=ExtractedPlace(name="Shibuya loft", city="Tokyo"),
            lodging=ExtractedLodging(property_type="vacation_rental", guests=4),
        )],
    )
    resp = client.post(
        "/received/images/process", files={"file": ("stay.png", PNG, "image/png")}, headers=sun
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["outcome"] == "placed"
    item = client.get(f"/trips/{trip['id']}/items", headers=sun).json()[0]
    assert (item["starts_at"], item["ends_at"]) == ("2026-04-11T15:00:00", "2026-04-14T11:00:00")
    assert item["lodging"]["property_type"] == "vacation_rental"
    assert item["lodging"]["guests"] == 4


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
    assert body["outcome"] == "skipped"
    assert body["receipt"]["status"] == "rejected"
