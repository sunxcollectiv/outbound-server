"""Retrieve stage: fetch received emails from Resend and store them, nothing
more — no sender check, no extraction. Stored rows start at status `received`;
the extract stage advances them.

Both entry points (single-by-id and recent-list) go through `_upsert_received`,
keyed on the unique `resend_email_id` so re-retrieving is idempotent. The
single-email retrieve has the full body and persists it in `raw`; the list has
metadata only.
"""
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import EmailReceipt, EmailReceiptStatus
from app.services.mail_processing import InboundEmail
from app.services.resend import ResendClient


def _upsert_received(
    session: Session, inbound: InboundEmail, raw: dict[str, Any] | None = None
) -> EmailReceipt:
    """Insert a `received` receipt, keyed on resend_email_id. A row we've already
    seen is left as-is (its status/metadata aren't rewound) — except that a
    re-fetch carrying the body refreshes `raw`, so the single-email retrieve can
    fill in a body first seen without one."""
    values = {
        "id": uuid.uuid4(),
        "resend_email_id": inbound.email_id,
        "status": EmailReceiptStatus.RECEIVED,
        "from_address": inbound.from_address,
        "subject": inbound.subject,
        "message_id": inbound.message_id,
        "received_at": inbound.received_at,
        "raw": raw,
    }
    stmt = pg_insert(EmailReceipt).values(**values)
    if raw is not None:
        stmt = stmt.on_conflict_do_update(
            index_elements=["resend_email_id"],
            set_={"raw": stmt.excluded.raw, "updated_at": datetime.now(UTC)},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["resend_email_id"])
    session.execute(stmt)
    return session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == inbound.email_id)
    )


def store_received_email(session: Session, email: dict[str, Any]) -> EmailReceipt | None:
    """Store one fully-retrieved email (Resend's retrieve response, body and
    all). None if the payload has no id."""
    inbound = InboundEmail.from_list_item(email)
    if inbound is None:
        return None
    return _upsert_received(session, inbound, raw=email)


def fetch_recent(
    client: ResendClient, count: int, *, page_size: int = 100
) -> list[dict[str, Any]]:
    """The most recent `count` emails Resend has received, newest first. Resend
    caps a page at 100 (and has no time filter), so we page back by id cursor until
    we've gathered `count` of them or run out."""
    collected: list[dict[str, Any]] = []
    before: str | None = None
    while len(collected) < count:
        listing = client.list_received_emails(
            limit=min(page_size, count - len(collected)), before=before
        )
        batch = listing.get("data") or []
        if not batch:
            break
        collected.extend(item for item in batch if isinstance(item, dict))
        last_id = batch[-1].get("id") if isinstance(batch[-1], dict) else None
        if not listing.get("has_more") or not last_id:
            break
        before = last_id
    return collected[:count]


def store_received_list(session: Session, emails: list[Any]) -> list[EmailReceipt]:
    """Store a page of listed emails (metadata only — the list has no body)."""
    stored: list[EmailReceipt] = []
    seen: set[str] = set()
    for item in emails:
        if not isinstance(item, dict):
            continue
        inbound = InboundEmail.from_list_item(item)
        if inbound is None or inbound.email_id in seen:
            continue
        seen.add(inbound.email_id)
        stored.append(_upsert_received(session, inbound))
    return stored
