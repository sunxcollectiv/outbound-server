"""Processing for inbound (received) email — the step that turns a raw Resend
email into an EmailReceipt, running the pipeline and deduping on the email id.

Everything here is deliberately framework-free and reusable across delivery
paths: the manual pull (`POST /trips/process-mail`) normalizes each listed email
to `InboundEmail` and calls `record_inbound_emails`; a future `email.received`
webhook will build the same `InboundEmail` from its payload and call the same
function. The pipeline itself (`_run_pipeline`) is where the ordered steps live,
so adding categorize/extract later touches one place, both paths get it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmailReceipt, EmailReceiptStatus, User


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


def _run_pipeline(session: Session, inbound: InboundEmail) -> dict[str, Any]:
    """Run the processing steps for one not-yet-seen email and return the
    receipt fields they decide. Ordered; later steps appended here.

      1. Sender account check — the email must come from a registered user's own
         address, else it's ignored (no user). A matched email is left pending
         for the later steps.
      (future) 2. categorize   3. extract into trip items → processed/failed
    """
    user = resolve_sender(session, inbound.from_address)
    return {
        "status": EmailReceiptStatus.PENDING if user is not None else EmailReceiptStatus.IGNORED,
        "user_id": user.id if user is not None else None,
    }


def record_inbound_emails(
    session: Session, inbounds: list[InboundEmail]
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
    rows = [
        {
            "id": uuid.uuid4(),
            "resend_email_id": inbound.email_id,
            "from_address": inbound.from_address,
            "subject": inbound.subject,
            "message_id": inbound.message_id,
            "received_at": inbound.received_at,
            **_run_pipeline(session, inbound),
        }
        for email_id, inbound in by_id.items()
        if email_id not in seen
    ]

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
