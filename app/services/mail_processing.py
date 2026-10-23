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
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


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
