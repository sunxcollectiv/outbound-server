from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.email_receipt import EmailReceiptStatus

if TYPE_CHECKING:
    from app.models.user import User


class ImageReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An uploaded image (e.g. a booking screenshot) in the same received-item
    pipeline as email: ``received`` → ``extracted`` → ``processed``. Its own
    table (``r_images``), standalone from EmailReceipt but reusing the status
    lifecycle and the ``extracted``/import fields.

    The image bytes live inline in ``data`` (bytea) — fine for screenshots;
    object storage is a later swap. There's no sender/account step: the "sender"
    is simply the authenticated uploader (``user_id``, set at upload), so an
    image never goes ``ignored``. The extract stage (Claude vision) and the
    import stage are the same shape as email's.
    """

    # "r_" groups the received-source tables (r_email_receipts, r_images, …).
    __tablename__ = "r_images"

    # The uploader — the owner of this image; always set.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # The trip it was imported into (set when status reaches `processed`).
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[EmailReceiptStatus] = mapped_column(
        str_enum(EmailReceiptStatus, "received_status"), default=EmailReceiptStatus.RECEIVED
    )
    # The uploaded image itself.
    data: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(100))
    filename: Mapped[str | None] = mapped_column(String(255))
    byte_size: Mapped[int] = mapped_column(Integer)
    # sha256 of the bytes — a hook for the future cross-source content dedup.
    # Not unique: re-uploading the same image is allowed for now.
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # The Claude classify+extract result (same Extraction shape as email).
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

    user: Mapped[User] = relationship()
