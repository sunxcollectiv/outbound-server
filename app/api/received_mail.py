from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.models import EmailReceipt, EmailReceiptStatus
from app.schemas import ImportResult, ReceivedMailRead
from app.services.mail_extraction import (
    MailExtractionNotConfigured,
    MailExtractor,
    get_mail_extractor,
)
from app.services.mail_import import import_receipt
from app.services.mail_processing import EXTRACTABLE_STATUSES, extract_receipt
from app.services.received_mail import store_received_email, store_received_list
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# The inbound-mail pipeline, as independently-callable stages. Retrieve ("" and
# "/{id}") fetches from Resend and stores; extract ("/extract" and
# "/extract/{id}") runs the sender check + Claude classify/extract on stored
# rows. The /extract routes are declared BEFORE /{email_id} so "extract" isn't
# captured as an email id.
router = APIRouter(prefix="/received/mail", tags=["received-mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]
MailExtractorDep = Annotated[MailExtractor, Depends(get_mail_extractor)]


def _resend(call):
    try:
        return call()
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except ResendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None


def _extract(session: SessionDep, receipt: EmailReceipt, resend, extractor) -> None:
    try:
        extract_receipt(session, receipt, resend=resend, extractor=extractor)
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except MailExtractionNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Claude (email extraction) is not configured"
        ) from None


# --- retrieve ---


@router.post("", response_model=list[ReceivedMailRead])
def retrieve_recent(
    _: CurrentUser,
    session: SessionDep,
    resend: ResendDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    before: Annotated[str | None, Query(description="email-id cursor: older than this id")] = None,
    after: Annotated[str | None, Query(description="email-id cursor: newer than this id")] = None,
) -> list[EmailReceipt]:
    """Retrieve a page of recent received emails and store each (metadata only;
    the list carries no body). Newest first. `limit` bounds the page; `before`/
    `after` page further back by email id (Resend has no time filter)."""
    listing = _resend(lambda: resend.list_received_emails(limit=limit, before=before, after=after))
    receipts = store_received_list(session, listing.get("data") or [])
    session.commit()
    return receipts


# --- extract (declared before /{email_id}) ---


@router.post("/extract", response_model=list[ReceivedMailRead])
def extract_recent(
    _: CurrentUser,
    session: SessionDep,
    resend: ResendDep,
    extractor: MailExtractorDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[EmailReceipt]:
    """Extract the backlog: process every receipt still awaiting it (status
    `received` or `pending`), oldest first, up to `limit`. Terminal rows
    (processed/rejected/ignored) are left alone, so this is safe to re-run."""
    receipts = list(
        session.scalars(
            select(EmailReceipt)
            .where(EmailReceipt.status.in_(EXTRACTABLE_STATUSES))
            .order_by(EmailReceipt.created_at)
            .limit(limit)
        )
    )
    for receipt in receipts:
        _extract(session, receipt, resend, extractor)
    session.commit()
    return receipts


@router.post("/extract/{email_id}", response_model=ReceivedMailRead)
def extract_one(
    email_id: str,
    _: CurrentUser,
    session: SessionDep,
    resend: ResendDep,
    extractor: MailExtractorDep,
    force: Annotated[bool, Query(description="re-extract even a terminal receipt")] = False,
) -> EmailReceipt:
    """Extract one received email by id. By default a terminal receipt
    (processed/rejected/ignored) is returned unchanged; pass `force=true` to
    re-run it."""
    receipt = session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id)
    )
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Received email not found")
    if force or receipt.status in EXTRACTABLE_STATUSES:
        _extract(session, receipt, resend, extractor)
        session.commit()
    return receipt


# --- import (declared before /{email_id}) ---


@router.post("/import", response_model=list[ImportResult])
def import_recent(
    _: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[ImportResult]:
    """Import the backlog: every `extracted` receipt, oldest first, up to `limit`.
    Places the ones that uniquely match a trip; the rest come back `no_match`/
    `ambiguous` and stay `extracted` (safe to re-run once more trips exist)."""
    receipts = session.scalars(
        select(EmailReceipt)
        .where(EmailReceipt.status == EmailReceiptStatus.EXTRACTED)
        .order_by(EmailReceipt.created_at)
        .limit(limit)
    )
    results = [import_receipt(session, r) for r in receipts]
    session.commit()
    return results


@router.post("/import/{email_id}", response_model=ImportResult)
def import_one(email_id: str, _: CurrentUser, session: SessionDep) -> ImportResult:
    """Import one extracted email into its matching trip. A receipt that isn't
    `extracted` comes back `skipped` (no double-create of an already-processed one)."""
    receipt = session.scalar(
        select(EmailReceipt).where(EmailReceipt.resend_email_id == email_id)
    )
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Received email not found")
    result = import_receipt(session, receipt)
    session.commit()
    return result


# --- retrieve one (the catch-all /{email_id}, declared last) ---


@router.post("/{email_id}", response_model=ReceivedMailRead)
def retrieve_one(
    email_id: str, _: CurrentUser, session: SessionDep, resend: ResendDep
) -> EmailReceipt:
    """Retrieve one received email by id and store it, body and all (in `raw`)."""
    email = _resend(lambda: resend.get_received_email(email_id))
    receipt = store_received_email(session, email)
    if receipt is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Resend returned an email without an id")
    session.commit()
    return receipt
