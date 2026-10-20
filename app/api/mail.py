from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas import ProcessMailResponse, ReceiptInfo
from app.services.mail_processing import InboundEmail, record_inbound_emails
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# Mounted under /trips: this is where received mail turns into trip items. For
# now the pipeline runs end-to-end on each manual pull (its only step so far is
# the sender account check); the per-email work lives in app.services
# .mail_processing so a future email.received webhook reuses it unchanged.
router = APIRouter(prefix="/trips", tags=["mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]


@router.post("/process-mail", response_model=ProcessMailResponse)
def process_mail(_: CurrentUser, session: SessionDep, resend: ResendDep) -> ProcessMailResponse:
    """Pull recent inbound email from Resend and process each one. Returns
    Resend's response as-is plus, per email, a receipt: its pipeline status, the
    account it was matched to, and whether this call first recorded it. Emails
    from senders without an account are ignored; the sender just resends from
    their registered address if they got it wrong."""
    try:
        listing = resend.list_received_emails()
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except ResendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None

    emails = listing.get("data") or []
    inbounds = [
        inbound
        for inbound in (
            InboundEmail.from_list_item(e) for e in emails if isinstance(e, dict)
        )
        if inbound is not None
    ]
    recorded = record_inbound_emails(session, inbounds)
    receipts = {
        email_id: ReceiptInfo(
            status=r.receipt.status,
            user_id=r.receipt.user_id,
            newly_recorded=r.newly_recorded,
        )
        for email_id, r in recorded.items()
    }
    session.commit()
    return ProcessMailResponse(emails=emails, receipts=receipts)
