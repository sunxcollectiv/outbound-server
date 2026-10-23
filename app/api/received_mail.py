from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.models import EmailReceipt
from app.schemas import ReceivedMailRead
from app.services.received_mail import store_received_email, store_received_list
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# The retrieve stage of the inbound-mail pipeline: fetch from Resend and store,
# nothing else (no sender check, no extraction — those are the extract stage).
# NOTE: when the /extract routes land, declare them before "/{email_id}" so
# "extract" isn't captured as an email id.
router = APIRouter(prefix="/received/mail", tags=["received-mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]


def _resend(call):
    try:
        return call()
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except ResendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None


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
