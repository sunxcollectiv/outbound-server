import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import CurrentUser, SessionDep
from app.models import EmailReceipt, EmailReceiptStatus
from app.schemas import ProcessMailResponse, ReceiptInfo
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# Mounted under /trips: this is where received mail will eventually turn into
# trip items. For now it pulls recent inbound email and records a receipt per
# email so the same one is never processed twice (see EmailReceipt).
router = APIRouter(prefix="/trips", tags=["mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]


def _parse_received_at(value: Any) -> datetime | None:
    """Resend's ISO timestamp (…Z) → aware datetime; None if absent/unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _record_receipts(session: SessionDep, emails: list[Any]) -> dict[str, ReceiptInfo]:
    """Record a `seen` receipt for every pulled email we haven't seen before and
    report each one's status. New rows are inserted with ON CONFLICT DO NOTHING
    (keyed by the unique resend_email_id) so a concurrent pull or webhook racing
    the same email can't double-insert; RETURNING tells us exactly which rows
    this call created, which is what `newly_recorded` reflects."""
    by_id = {e["id"]: e for e in emails if isinstance(e, dict) and e.get("id")}
    if not by_id:
        return {}
    ids = list(by_id)

    existing = set(
        session.scalars(
            select(EmailReceipt.resend_email_id).where(EmailReceipt.resend_email_id.in_(ids))
        )
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "resend_email_id": rid,
            "status": EmailReceiptStatus.SEEN,
            "from_address": email.get("from"),
            "subject": email.get("subject"),
            "message_id": email.get("message_id"),
            "received_at": _parse_received_at(email.get("created_at")),
        }
        for rid, email in by_id.items()
        if rid not in existing
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
        session.commit()

    statuses = {
        r.resend_email_id: r.status
        for r in session.scalars(
            select(EmailReceipt).where(EmailReceipt.resend_email_id.in_(ids))
        )
    }
    return {
        rid: ReceiptInfo(status=statuses[rid], newly_recorded=rid in inserted)
        for rid in by_id
        if rid in statuses
    }


@router.post("/process-mail", response_model=ProcessMailResponse)
def process_mail(_: CurrentUser, session: SessionDep, resend: ResendDep) -> ProcessMailResponse:
    """Pull recent inbound email from Resend — the first step toward parsing
    forwarded travel confirmations into trip items. Returns Resend's response
    as-is plus a per-email receipt status, so the caller can tell which emails
    are new and which have been seen/processed before."""
    try:
        listing = resend.list_received_emails()
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except ResendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None

    emails = listing.get("data") or []
    receipts = _record_receipts(session, emails)
    return ProcessMailResponse(emails=emails, receipts=receipts)
