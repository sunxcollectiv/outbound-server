from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class EmailReceiptStatus(StrEnum):
    SEEN = "seen"  # recorded on sight; not yet acted on
    PROCESSED = "processed"  # parsed into trip item(s) — written by a later step
    FAILED = "failed"  # processing was attempted and failed — later step


class EmailReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per inbound email we've seen, keyed by its Resend id, so the same
    email is never processed twice.

    Written receipt-on-sight: the moment an email turns up — pulled via
    ``/trips/process-mail`` today, or delivered by an ``email.received`` webhook
    later — we record it with ``status=seen``. Both paths carry the same
    ``resend_email_id`` (the webhook's ``data.email_id`` equals the list/retrieve
    ``id``), so one ledger dedupes across both. The unique constraint on
    ``resend_email_id`` is the dedup guard: a duplicate insert conflicts rather
    than racing to a second row.

    The ``from_address``/``subject``/``message_id``/``received_at`` snapshot comes
    free from the list response — enough to show "already processed: …" without a
    re-fetch. Full bodies/attachments are deliberately not stored here.
    """

    # "r_" groups the received-mail tables (receipts, and later screenshots/
    # photos) together in the database listing.
    __tablename__ = "r_email_receipts"

    resend_email_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[EmailReceiptStatus] = mapped_column(
        str_enum(EmailReceiptStatus, "email_receipt_status"),
        default=EmailReceiptStatus.SEEN,
    )
    from_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(1024))
    message_id: Mapped[str | None] = mapped_column(String(998))
    # When Resend received the email (its own created_at), distinct from this
    # row's created_at (when we first saw it).
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a later step marks the email processed; null while status=seen.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
