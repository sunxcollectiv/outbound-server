from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser
from app.services.resend import (
    ResendClient,
    ResendError,
    ResendNotConfigured,
    get_resend_client,
)

# Mounted under /trips: this is where received mail will eventually turn into
# trip items. For now it's a bare first step that just surfaces Resend's inbox.
router = APIRouter(prefix="/trips", tags=["mail"])

ResendDep = Annotated[ResendClient, Depends(get_resend_client)]


@router.post("/process-mail")
def process_mail(_: CurrentUser, resend: ResendDep) -> dict[str, Any]:
    """Pull recent inbound email from Resend and return its response as-is — the
    first step toward parsing forwarded travel confirmations into trip items."""
    try:
        return resend.list_received_emails()
    except ResendNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Resend is not configured"
        ) from None
    except ResendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
