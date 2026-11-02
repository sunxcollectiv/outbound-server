import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.models import EmailReceiptStatus, ImageReceipt
from app.schemas import ImageImportResult, ImageReceiptRead
from app.services.image_extraction import (
    ImageExtractionNotConfigured,
    ImageExtractor,
    get_image_extractor,
)
from app.services.image_processing import EXTRACTABLE_STATUSES, extract_image
from app.services.receipt_import import ImportOutcome, import_receipt, process_receipt

# The image pipeline mirrors the mail one (received → extracted → processed).
# `router` is the one user-facing endpoint — POST /received/images/process, the
# screenshot-to-trip flow a signed-in user runs on their own trips. The
# step-by-step stages (upload / extract / import) are admin/dev tooling and live
# on `admin_router`, mounted under /admin (service-token only, no user behind
# them), so a dev can walk a receipt through the pipeline.
router = APIRouter(prefix="/received/images", tags=["received-images"])
admin_router = APIRouter(prefix="/received/images", tags=["received-images"])

ImageExtractorDep = Annotated[ImageExtractor, Depends(get_image_extractor)]

# Screenshots are small; cap the inline-stored bytes so a huge upload can't bloat
# a row. Object storage is the eventual home.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _extract(session: SessionDep, image: ImageReceipt, extractor: ImageExtractor) -> None:
    try:
        extract_image(session, image, extractor=extractor)
    except ImageExtractionNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Claude (image extraction) is not configured"
        ) from None


def _store_upload(
    session: SessionDep, user_id: uuid.UUID | None, file: UploadFile
) -> ImageReceipt:
    """Validate an upload and persist it as a `received` image. `user_id` is the
    uploader (whose trips import matches against) for the user-facing /process,
    or None for the admin dev endpoints. Shared by /upload and /process; does
    NOT commit."""
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
        user_id=user_id,
        status=EmailReceiptStatus.RECEIVED,
        data=data,
        content_type=content_type,
        filename=file.filename,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(image)
    return image


# --- user-facing: upload + extract + import in one call ---


@router.post(
    "/process",
    response_model=ImageImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Process a booking screenshot",
)
def process_upload(
    user: CurrentUser,
    session: SessionDep,
    extractor: ImageExtractorDep,
    file: UploadFile = File(...),
) -> ImportOutcome:
    """Upload a screenshot and run the whole pipeline in one call — store,
    extract (Claude vision), and import into the caller's matching trip. Returns
    the final outcome (`placed`/`no_match`/`ambiguous`, or `skipped` with a
    `rejected` receipt when it isn't a confident booking) so the client can
    update its progress pill and refresh the itinerary in one round-trip."""
    image = _store_upload(session, user.id, file)
    session.flush()  # assign the id before we process
    result = process_receipt(
        session, image, extract=lambda: _extract(session, image, extractor)
    )
    session.commit()
    return result


# --- admin/dev: the pipeline stages, one at a time (no user behind them) ---


@admin_router.post(
    "/upload",
    response_model=ImageReceiptRead,
    status_code=status.HTTP_201_CREATED,
    summary="[admin] Upload image",
)
def upload_image(session: SessionDep, file: UploadFile = File(...)) -> ImageReceipt:
    """Upload one image into the received-item pipeline and record it as
    `received` (no uploader — import won't match a trip without one; use
    /received/images/process for the real user flow). Extraction is a later call."""
    image = _store_upload(session, None, file)
    session.commit()
    session.refresh(image)
    return image


@admin_router.post(
    "/extract",
    response_model=list[ImageReceiptRead],
    summary="[admin] Extract image backlog",
)
def extract_recent(
    session: SessionDep,
    extractor: ImageExtractorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[ImageReceipt]:
    """Extract the backlog: run vision on every image still awaiting it (status
    `received` or `pending`), oldest first, up to `limit`. Terminal rows
    (processed/rejected) are left alone, so this is safe to re-run."""
    images = list(
        session.scalars(
            select(ImageReceipt)
            .where(ImageReceipt.status.in_(EXTRACTABLE_STATUSES))
            .order_by(ImageReceipt.created_at)
            .limit(limit)
        )
    )
    for image in images:
        _extract(session, image, extractor)
    session.commit()
    return images


@admin_router.post(
    "/extract/{image_id}",
    response_model=ImageReceiptRead,
    summary="[admin] Extract one image",
)
def extract_one(
    image_id: uuid.UUID,
    session: SessionDep,
    extractor: ImageExtractorDep,
    force: Annotated[bool, Query(description="re-extract even a terminal image")] = False,
) -> ImageReceipt:
    """Extract one uploaded image by id. By default a terminal image
    (processed/rejected) is returned unchanged; pass `force=true` to re-run it."""
    image = session.get(ImageReceipt, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    if force or image.status in EXTRACTABLE_STATUSES:
        _extract(session, image, extractor)
        session.commit()
    return image


@admin_router.post(
    "/import",
    response_model=list[ImageImportResult],
    summary="[admin] Import image backlog",
)
def import_recent(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[ImportOutcome]:
    """Import the backlog: every `extracted` image, oldest first, up to `limit`.
    Places the ones that uniquely match a trip; the rest come back `no_match`/
    `ambiguous` and stay `extracted` (safe to re-run once more trips exist)."""
    images = session.scalars(
        select(ImageReceipt)
        .where(ImageReceipt.status == EmailReceiptStatus.EXTRACTED)
        .order_by(ImageReceipt.created_at)
        .limit(limit)
    )
    results = [import_receipt(session, image) for image in images]
    session.commit()
    return results


@admin_router.post(
    "/import/{image_id}",
    response_model=ImageImportResult,
    summary="[admin] Import one image",
)
def import_one(image_id: uuid.UUID, session: SessionDep) -> ImportOutcome:
    """Import one extracted image into its matching trip. An image that isn't
    `extracted` comes back `skipped` (no double-create of an already-processed one)."""
    image = session.get(ImageReceipt, image_id)
    if image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    result = import_receipt(session, image)
    session.commit()
    return result
