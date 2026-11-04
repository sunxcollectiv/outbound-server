import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.models import CreditEntry
from app.schemas import CreditPurchaseCreate, CreditsRead
from app.services import credits
from app.services.credits import PurchaseClaimed, PurchaseForOtherAccount
from app.services.storekit import (
    PurchaseNotVerified,
    StoreKitVerifier,
    get_storekit_verifier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credits", tags=["credits"])

DEFAULT_PAGE = 25
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[
    uuid.UUID | None, Query(description="entry-id cursor: older than this one")
]
StoreKitDep = Annotated[StoreKitVerifier, Depends(get_storekit_verifier)]

# Why a purchase can be turned down, and how that's answered.
_REFUSALS: dict[type[Exception], int] = {
    PurchaseNotVerified: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PurchaseForOtherAccount: status.HTTP_403_FORBIDDEN,
    PurchaseClaimed: status.HTTP_409_CONFLICT,
}


def read_statement(
    session: Session,
    user_id: uuid.UUID,
    limit: int = DEFAULT_PAGE,
    before: uuid.UUID | None = None,
) -> CreditsRead:
    """A user's balance and a page of their ledger. Shared with the admin view."""
    before_seq = None
    if before is not None:
        before_seq = session.scalar(
            select(CreditEntry.seq).where(CreditEntry.id == before, CreditEntry.user_id == user_id)
        )
        if before_seq is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown cursor")
    lines, more = credits.statement(session, user_id, limit=limit, before_seq=before_seq)
    return CreditsRead(
        balance_cents=credits.balance(session, user_id),
        entries=lines,
        next_cursor=lines[-1].id if more else None,
    )


@router.get("", response_model=CreditsRead)
def read_credits(
    user: CurrentUser,
    session: SessionDep,
    limit: PageLimit = DEFAULT_PAGE,
    before: PageCursor = None,
) -> CreditsRead:
    """The caller's balance and their ledger, newest first, a page at a time:
    pass the returned `next_cursor` as `before` to go further back."""
    return read_statement(session, user.id, limit, before)


@router.post(
    "/purchases",
    response_model=CreditsRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"description": "Already credited; nothing changed"}},
)
def purchase_credits(
    payload: CreditPurchaseCreate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    verifier: StoreKitDep,
) -> CreditsRead:
    """Credits an App Store purchase to the caller and returns their credits
    after it, as GET /credits does. Safe to retry: a purchase already
    credited to the caller answers 200 and changes nothing, so the app can
    finish the transaction. A transaction that doesn't verify, isn't a
    credits product, or was refunded is a 422; one bought for another
    account (its appAccountToken) is a 403, and one already credited to
    another account a 409."""
    try:
        purchase = verifier.verify(payload.signed_transaction)
        credited = credits.credit_purchase(session, user.id, purchase)
        session.commit()
    except tuple(_REFUSALS) as exc:
        session.rollback()
        logger.warning("Refused App Store purchase from user %s: %s", user.id, exc)
        raise HTTPException(_REFUSALS[type(exc)], str(exc)) from None
    if not credited:
        response.status_code = status.HTTP_200_OK
    return read_statement(session, user.id)
