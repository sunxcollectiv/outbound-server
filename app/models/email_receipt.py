from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from app.models.user import User


class EmailReceiptStatus(StrEnum):
    PENDING = "pending"  # sender matched; awaiting later steps (categorize/extract)
    IGNORED = "ignored"  # sender has no account — not processed
    PROCESSED = "processed"  # parsed into trip item(s) — set by a later step
    FAILED = "failed"  # processing was attempted and errored — later step


class EmailReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per inbound email we've seen, keyed by its Resend id, so the same
    email is never processed twice.

    Recorded while processing an email — pulled via ``/trips/process-mail``
    today, or delivered by an ``email.received`` webhook later. Both paths carry
    the same ``resend_email_id`` (the webhook's ``data.email_id`` equals the
    list/retrieve ``id``), so one ledger dedupes across both; the unique
    constraint on it is the dedup guard.

    ``status`` reflects where the email got to in the pipeline: ``ignored`` when
    its sender isn't a registered user (``user_id`` null), otherwise ``pending``
    until the later categorize/extract steps advance it to
    ``processed``/``failed``. ``user_id`` is the account the email belongs to —
    the forwarder, matched on their own address (aliases come later).

    The ``from_address``/``subject``/``message_id``/``received_at`` snapshot comes
    free from the list response — enough to audit without a re-fetch. Full
    bodies/attachments are deliberately not stored here.
    """

    # "r_" groups the received-mail tables (receipts, and later screenshots/
    # photos) together in the database listing.
    __tablename__ = "r_email_receipts"

    resend_email_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[EmailReceiptStatus] = mapped_column(
        str_enum(EmailReceiptStatus, "email_receipt_status"),
        default=EmailReceiptStatus.PENDING,
    )
    # The account this email belongs to (the matched sender); null when ignored.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    from_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(1024))
    message_id: Mapped[str | None] = mapped_column(String(998))
    # When Resend received the email (its own created_at), distinct from this
    # row's created_at (when we first saw it).
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a later step marks the email processed; null until then.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship()
