from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from app.models.user import User


class EmailReceiptStatus(StrEnum):
    RECEIVED = "received"  # retrieved from Resend and stored; not yet processed
    PENDING = "pending"  # matched; queued for processing / awaiting a retry (the worklist)
    PROCESSING = "processing"  # picked up by a worker, in-flight (guards double-pickup)
    PROCESSED = "processed"  # extracted into trip item(s) — terminal success
    REJECTED = "rejected"  # terminal failure: retry cap hit, or a permanent reason
    IGNORED = "ignored"  # sender has no account — terminal, never processed

    # NOTE — stuck-process cleanup (don't forget): a row can be left in
    # `processing` if a worker dies mid-flight. Once processing is async/queued,
    # add a reaper that reclaims rows sitting in `processing` past a timeout back
    # to `pending` (so they re-enter the worklist). Today's processing runs
    # inline within one request, so it can't actually get stuck — this is a
    # when-we-go-async item.


class EmailReceipt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per inbound email we've seen, keyed by its Resend id, so the same
    email is never processed twice.

    Created by the retrieve stage (``/received/mail``), which only fetches from
    Resend and stores — no sender check, no extraction. A later extract stage
    advances it. An ``email.received`` webhook will share the ledger too: both it
    and the retrieve path carry the same ``resend_email_id`` (the webhook's
    ``data.email_id`` equals the list/retrieve ``id``), so the unique constraint
    on it is the one dedup guard.

    ``status`` tracks where the email got to (see ``EmailReceiptStatus``):
    ``received`` on retrieval; then the extract stage sets ``ignored`` (sender
    isn't a registered user, ``user_id`` null) or runs the pipeline —
    ``processing`` while in-flight, ``processed`` on success, or back to
    ``pending`` on a transient error (each logged to ``failures``) until the 3rd
    failure tips it to ``rejected``. ``user_id`` is the account the email belongs
    to — the forwarder, matched on their own address (aliases come later).

    The ``from_address``/``subject``/``message_id``/``received_at`` snapshot comes
    from the list response; ``raw`` holds the full body once the single-email
    retrieve has fetched it.
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
    # One entry per failed processing attempt, e.g.
    # {"at": "<iso>", "step": "categorize", "reason": "..."}. The retry cap reads
    # off its length: 3 failures → status flips to `rejected`.
    failures: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # The full received email as Resend returned it from the retrieve endpoint
    # (text/html body, headers, attachment metadata). Filled by the single-email
    # retrieve; null for ones only seen in the list. The extract stage reads the
    # body from here rather than re-calling Resend.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The AI classify+extract result (an app.services.mail_extraction.Extraction
    # as a dict: category, confidence, summary, items). Null until extracted; the
    # input to the later trip-item-creation step. Not the trip items themselves.
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    user: Mapped[User | None] = relationship()
