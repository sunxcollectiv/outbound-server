import hashlib

from sqlalchemy import func, select

from app.models import EmailReceiptStatus, ImageReceipt

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(ImageReceipt))


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
