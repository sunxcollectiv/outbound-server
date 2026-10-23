"""Processing for inbound (received) email — turns a raw Resend email into an
EmailReceipt, running the pipeline and deduping on the email id.

Framework-free and reusable across delivery paths: the manual pull
(`POST /trips/process-mail`) normalizes each listed email to `InboundEmail` and
calls `record_inbound_emails`; a future `email.received` webhook will build the
same `InboundEmail` from its payload and call the same function. The ordered
steps live in `_run_pipeline`, so adding later steps touches one place and both
paths get them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmailReceipt, EmailReceiptStatus, User
from app.services.mail_extraction import (
    MailExtractionError,
    MailExtractionNotConfigured,
    MailExtractor,
)
from app.services.resend import ResendClient, ResendError, ResendNotConfigured

# A matched email below this extraction confidence is rejected rather than
# trusted — bad trip data costs more than asking the user to resend.
CONFIDENCE_THRESHOLD = 0.6
# After this many failed attempts an email is rejected instead of retried.
MAX_ATTEMPTS = 3


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
    """The fields processing needs, normalized from whichever Resend shape
    delivered the email, so the pipeline doesn't care where it came from.
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


@dataclass(frozen=True)
class RecordedEmail:
    receipt: EmailReceipt
    newly_recorded: bool  # False when this email had already been recorded


def _failure(step: str, reason: object) -> dict[str, Any]:
    return {"at": datetime.now(UTC).isoformat(), "step": step, "reason": str(reason)[:500]}


def _body_text(email: dict[str, Any]) -> str | None:
    """Prefer the plaintext body (cheaper tokens); fall back to HTML only when a
    text part is absent."""
    return email.get("text") or email.get("html")


def _run_pipeline(
    session: Session,
    inbound: InboundEmail,
    *,
    resend: ResendClient,
    extractor: MailExtractor,
) -> dict[str, Any]:
    """Run the ordered processing steps for one not-yet-seen email and return the
    receipt fields they decide.

      1. Sender account check — must come from a registered user's own address,
         else `ignored` (and we stop here, so unmatched mail never hits Claude).
      2. Classify + extract — fetch the body and run one Claude pass. A booking
         we're confident in → `processed` with the extraction stored; a
         non-booking or low-confidence result → `rejected`; a transient
         Resend/Claude error is logged to `failures` and the email stays
         `pending` (or `rejected` once it has failed MAX_ATTEMPTS times), for a
         later retry pass to pick up.
      (future) 3. turn the extraction into trip items.

    A missing API key is a misconfiguration, not a per-email failure, so it
    propagates (the endpoint turns it into a 503).
    """
    user = resolve_sender(session, inbound.from_address)
    if user is None:
        return {"status": EmailReceiptStatus.IGNORED, "user_id": None}

    base: dict[str, Any] = {"user_id": user.id}

    try:
        email = resend.get_received_email(inbound.email_id)
        extraction = extractor.extract(subject=inbound.subject, text_body=_body_text(email))
    except (ResendNotConfigured, MailExtractionNotConfigured):
        raise
    except (ResendError, MailExtractionError) as exc:
        failures = [_failure("extract", exc)]
        status = (
            EmailReceiptStatus.REJECTED
            if len(failures) >= MAX_ATTEMPTS
            else EmailReceiptStatus.PENDING
        )
        return {**base, "status": status, "failures": failures}

    now = datetime.now(UTC)
    rejected = extraction.category == "not_a_booking" or extraction.confidence < CONFIDENCE_THRESHOLD
    return {
        **base,
        "status": EmailReceiptStatus.REJECTED if rejected else EmailReceiptStatus.PROCESSED,
        "extracted": extraction.model_dump(),
        "processed_at": now,
    }


def record_inbound_emails(
    session: Session,
    inbounds: list[InboundEmail],
    *,
    resend: ResendClient,
    extractor: MailExtractor,
) -> dict[str, RecordedEmail]:
    """Process and record a batch of inbound emails, keyed by Resend email id.

    Idempotent: an email already in the ledger is left exactly as it was (its
    pipeline is not re-run), so a second pull or an at-least-once webhook retry
    is a no-op. New emails run `_run_pipeline` and are inserted with
    ON CONFLICT DO NOTHING on the unique resend_email_id, so two deliveries
    racing the same email can't double-insert; RETURNING tells us which rows this
    call actually created (`newly_recorded`). The caller commits.

    The manual pull passes the whole listing; a webhook will pass a single
    `InboundEmail` — same path either way.
    """
    by_id: dict[str, InboundEmail] = {}
    for inbound in inbounds:
        by_id.setdefault(inbound.email_id, inbound)
    if not by_id:
        return {}
    ids = list(by_id)

    seen = set(
        session.scalars(
            select(EmailReceipt.resend_email_id).where(EmailReceipt.resend_email_id.in_(ids))
        )
    )
    rows = []
    for email_id, inbound in by_id.items():
        if email_id in seen:
            continue
        decided = _run_pipeline(session, inbound, resend=resend, extractor=extractor)
        rows.append(
            {
                "id": uuid.uuid4(),
                "resend_email_id": email_id,
                "from_address": inbound.from_address,
                "subject": inbound.subject,
                "message_id": inbound.message_id,
                "received_at": inbound.received_at,
                "status": decided["status"],
                "user_id": decided.get("user_id"),
                "failures": decided.get("failures", []),
                "extracted": decided.get("extracted"),
                "processed_at": decided.get("processed_at"),
            }
        )

    inserted: set[str] = set()
    if rows:
        stmt = (
            pg_insert(EmailReceipt)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["resend_email_id"])
            .returning(EmailReceipt.resend_email_id)
        )
        inserted = set(session.scalars(stmt))

    receipts = {
        r.resend_email_id: r
        for r in session.scalars(
            select(EmailReceipt).where(EmailReceipt.resend_email_id.in_(ids))
        )
    }
    return {
        email_id: RecordedEmail(receipt=receipts[email_id], newly_recorded=email_id in inserted)
        for email_id in by_id
        if email_id in receipts
    }
