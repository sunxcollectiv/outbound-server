from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas import ProcessMailResponse, ReceiptInfo
from app.services.mail_extraction import (
    MailExtractionNotConfigured,
    MailExtractor,
    get_mail_extractor,
)
from app.services.mail_processing import InboundEmail, record_inbound_emails
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# Mounted under /trips: this is where received mail turns into trip items. The
# pipeline runs end-to-end on each manual pull (sender check, then a Claude
# classify+extract pass); the per-email work lives in app.services.mail_processing
# so a future email.received webhook reuses it unchanged.
router = APIRouter(prefix="/trips", tags=["mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]
MailExtractorDep = Annotated[MailExtractor, Depends(get_mail_extractor)]


@router.post("/process-mail", response_model=ProcessMailResponse)
def process_mail(
    _: CurrentUser,
    session: SessionDep,
    resend: ResendDep,
    extractor: MailExtractorDep,
) -> ProcessMailResponse:
    """Pull recent inbound email from Resend and process each one. Returns
    Resend's response as-is plus, per email, a receipt: its pipeline status, the
    account it was matched to, how Claude classified it, and whether this call
    first recorded it. Emails from senders without an account are ignored."""
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
    try:
        recorded = record_inbound_emails(session, inbounds, resend=resend, extractor=extractor)
    except MailExtractionNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Claude (email extraction) is not configured"
        ) from None

    receipts = {
        email_id: ReceiptInfo(
            status=r.receipt.status,
            user_id=r.receipt.user_id,
            category=(r.receipt.extracted or {}).get("category"),
            newly_recorded=r.newly_recorded,
        )
        for email_id, r in recorded.items()
    }
    session.commit()
    return ProcessMailResponse(emails=emails, receipts=receipts)
