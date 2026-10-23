import hashlib

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.deps import CurrentUser, SessionDep
from app.models import EmailReceiptStatus, ImageReceipt
from app.schemas import ImageReceiptRead

# The image pipeline mirrors the mail one (received → extracted → processed), but
# images arrive by upload instead of being fetched, so the retrieve stage is a
# single /upload endpoint. Extract (Claude vision) and import come next, mirroring
# mail; when the /extract and /import routes land, declare them before any /{id}.
router = APIRouter(prefix="/received/images", tags=["received-images"])

# Screenshots are small; cap the inline-stored bytes so a huge upload can't bloat
# a row. Object storage is the eventual home.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@router.post("/upload", response_model=ImageReceiptRead, status_code=status.HTTP_201_CREATED)
def upload_image(
    user: CurrentUser, session: SessionDep, file: UploadFile = File(...)
) -> ImageReceipt:
    """Upload one image (e.g. a booking screenshot) into the received-item
    pipeline. Stores the bytes and records it as `received`, owned by the
    uploader; extraction happens in a later call."""
    data = file.file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The upload is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Image is too large")
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only image uploads are accepted"
        )

    image = ImageReceipt(
        user_id=user.id,
        status=EmailReceiptStatus.RECEIVED,
        data=data,
        content_type=content_type,
        filename=file.filename,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(image)
    session.commit()
    session.refresh(image)
    return image
