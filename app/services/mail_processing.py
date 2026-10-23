"""Shared helpers for the inbound-mail pipeline: normalizing a Resend email into
the fields we care about, and matching a sender to an account.

The pipeline is split into independently-callable stages: retrieval
(`app/services/received_mail.py` → `/received/mail`) fetches from Resend and
stores; a later extract stage runs the sender check + Claude classify/extract on
stored rows. `InboundEmail` is the normalization both share; `resolve_sender` /
`extract_sender_email` are the sender-match step the extract stage uses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailReceipt, EmailReceiptStatus, User
from app.services.mail_extraction import (
    MailExtractionError,
    MailExtractionNotConfigured,
    MailExtractor,
)
from app.services.resend import ResendClient, ResendError, ResendNotConfigured


def extract_sender_email(raw_from: str | None) -> str | None:
    """The bare address from a From value — handles `Name <addr>` — lowercased
    to match how User.email is stored. None if there's no usable address."""
    if not raw_from:
        return None
    _, addr = parseaddr(raw_from)
    addr = addr.strip().lower()
    return addr if "@" in addr else None


def resolve_sender(session: Session, raw_from: str | None) -> User | None:
    """The registered user the email is from, if any. MVP: the forwarder's own
    From address, matched exactly against User.email. Aliases and
    trip+token@ addresses are a later step, so a user who forwards from a
    different address than they signed up with won't match yet."""
    addr = extract_sender_email(raw_from)
    if addr is None:
        return None
    return session.scalar(select(User).where(User.email == addr))


def _parse_received_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class InboundEmail:
    """The fields the pipeline needs, normalized from whichever Resend shape
    delivered the email, so stages don't care where it came from.
    `from_list_item` reads a received-email list/retrieve object (`id`, `from`,
    …); `from_webhook` reads an `email.received` payload's `data` (`email_id`,
    `from`, …)."""

    email_id: str
    from_address: str | None
    subject: str | None
    message_id: str | None
    received_at: datetime | None

    @classmethod
    def from_list_item(cls, item: dict[str, Any]) -> InboundEmail | None:
        email_id = item.get("id")
        if not email_id:
            return None
        return cls(
            email_id=email_id,
            from_address=item.get("from"),
            subject=item.get("subject"),
            message_id=item.get("message_id"),
            received_at=_parse_received_at(item.get("created_at")),
        )

    @classmethod
    def from_webhook(cls, data: dict[str, Any]) -> InboundEmail | None:
        email_id = data.get("email_id")
        if not email_id:
            return None
        return cls(
            email_id=email_id,
            from_address=data.get("from"),
            subject=data.get("subject"),
            message_id=data.get("message_id"),
            received_at=_parse_received_at(data.get("created_at")),
        )


# --- extract stage: run the pipeline on a stored receipt, in place ---

# A matched email below this extraction confidence is rejected rather than
# trusted — bad trip data costs more than asking the user to resend.
CONFIDENCE_THRESHOLD = 0.6
# After this many failed attempts an email is rejected instead of retried.
MAX_ATTEMPTS = 3
# The statuses the extract stage will (re)process; terminal ones are left alone.
EXTRACTABLE_STATUSES = (EmailReceiptStatus.RECEIVED, EmailReceiptStatus.PENDING)


def _failure(step: str, reason: object) -> dict[str, Any]:
    return {"at": datetime.now(UTC).isoformat(), "step": step, "reason": str(reason)[:500]}


def _body_text(raw: dict[str, Any]) -> str | None:
    """Prefer the plaintext body (cheaper tokens); fall back to HTML."""
    return raw.get("text") or raw.get("html")


def extract_receipt(
    session: Session,
    receipt: EmailReceipt,
    *,
    resend: ResendClient,
    extractor: MailExtractor,
) -> EmailReceipt:
    """Run the processing pipeline on one stored receipt, updating it in place.

      1. Sender check — must be from a registered user's own address, else
         `ignored` (we stop; no body fetch, no Claude call).
      2. Classify + extract — use the stored `raw` body, or fetch it from Resend
         and backfill `raw`, then run one Claude pass. A confident booking →
         `extracted` (+ `extracted`, `extracted_at`); a non-booking or
         low-confidence result → `rejected` (still stored); a transient
         Resend/Claude error → logged to `failures` and `pending`, or `rejected`
         once it has failed MAX_ATTEMPTS times. (`processed` is the later
         trip-item step, not this one.)

    A missing API key is a misconfiguration, not a per-email failure, so it
    propagates (the route turns it into a 503). The caller commits.
    """
    user = resolve_sender(session, receipt.from_address)
    if user is None:
        receipt.user_id = None
        receipt.status = EmailReceiptStatus.IGNORED
        return receipt
    receipt.user_id = user.id

    try:
        if receipt.raw is None:
            receipt.raw = resend.get_received_email(receipt.resend_email_id)
        extraction = extractor.extract(subject=receipt.subject, text_body=_body_text(receipt.raw))
    except (ResendNotConfigured, MailExtractionNotConfigured):
        raise
    except (ResendError, MailExtractionError) as exc:
        receipt.failures = [*receipt.failures, _failure("extract", exc)]
        receipt.status = (
            EmailReceiptStatus.REJECTED
            if len(receipt.failures) >= MAX_ATTEMPTS
            else EmailReceiptStatus.PENDING
        )
        return receipt

    receipt.extracted = extraction.model_dump()
    if extraction.category == "not_a_booking" or extraction.confidence < CONFIDENCE_THRESHOLD:
        receipt.status = EmailReceiptStatus.REJECTED
    else:
        receipt.status = EmailReceiptStatus.EXTRACTED
        receipt.extracted_at = datetime.now(UTC)
    return receipt
