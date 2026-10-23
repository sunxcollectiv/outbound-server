"""The extract stage for uploaded images: run Claude vision on a stored image and
advance its status in place.

The image counterpart of `app.services.mail_processing`. It's simpler than mail
in two ways: there's no sender check (the image's owner is the uploader, set at
upload, so an image never goes `ignored`) and no body fetch (the bytes are
already stored on the row). The confidence/failure/retry logic and the status
transitions mirror mail so the two pipelines behave the same — the constants are
duplicated here deliberately to keep the image path independent of the mail one.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import EmailReceiptStatus, ImageReceipt
from app.services.image_extraction import (
    ImageExtractionError,
    ImageExtractionNotConfigured,
    ImageExtractor,
)

# A booking below this extraction confidence is rejected rather than trusted —
# bad trip data costs more than asking the user to re-upload.
CONFIDENCE_THRESHOLD = 0.6
# After this many failed attempts an image is rejected instead of retried.
MAX_ATTEMPTS = 3
# The statuses the extract stage will (re)process; terminal ones are left alone.
EXTRACTABLE_STATUSES = (EmailReceiptStatus.RECEIVED, EmailReceiptStatus.PENDING)


def _failure(step: str, reason: object) -> dict[str, Any]:
    return {"at": datetime.now(UTC).isoformat(), "step": step, "reason": str(reason)[:500]}


def extract_image(
    session: Session, image: ImageReceipt, *, extractor: ImageExtractor
) -> ImageReceipt:
    """Run one Claude vision pass on a stored image, updating it in place: a
    confident booking → `extracted` (+ `extracted`, `extracted_at`); a non-booking
    or low-confidence result → `rejected` (still stored); a transient Claude error
    → logged to `failures` and `pending`, or `rejected` once it has failed
    MAX_ATTEMPTS times. (`processed` is the later trip-item step, not this one.)

    A missing API key is a misconfiguration, not a per-image failure, so it
    propagates (the route turns it into a 503). The caller commits.
    """
    try:
        extraction = extractor.extract(data=image.data, content_type=image.content_type)
    except ImageExtractionNotConfigured:
        raise
    except ImageExtractionError as exc:
        image.failures = [*image.failures, _failure("extract", exc)]
        image.status = (
            EmailReceiptStatus.REJECTED
            if len(image.failures) >= MAX_ATTEMPTS
            else EmailReceiptStatus.PENDING
        )
        return image

    image.extracted = extraction.model_dump()
    if extraction.category == "not_a_booking" or extraction.confidence < CONFIDENCE_THRESHOLD:
        image.status = EmailReceiptStatus.REJECTED
    else:
        image.status = EmailReceiptStatus.EXTRACTED
        image.extracted_at = datetime.now(UTC)
    return image
