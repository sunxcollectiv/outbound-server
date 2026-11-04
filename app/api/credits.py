import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.models import CreditEntry
from app.schemas import CreditPurchaseCreate, CreditsRead
from app.services import credits
from app.services.credits import PurchaseClaimed
from app.services.storekit import (
    PurchaseNotVerified,
    StoreKitVerifier,
    get_storekit_verifier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/credits", tags=["credits"])

PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[
    uuid.UUID | None, Query(description="entry-id cursor: older than this one")
]


def read_statement(
    session: Session, user_id: uuid.UUID, limit: int, before: uuid.UUID | None
) -> CreditsRead:
    """A user's balance and a page of their ledger. Shared with the admin view."""
    anchor = None
    if before is not None:
        anchor = session.scalar(
            select(CreditEntry).where(CreditEntry.id == before, CreditEntry.user_id == user_id)
        )
        if anchor is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown cursor")
    lines, more = credits.statement(session, user_id, limit=limit, before=anchor)
    return CreditsRead(
        balance_cents=credits.balance(session, user_id),
        entries=lines,
        next_cursor=lines[-1].id if more else None,
    )


@router.get("", response_model=CreditsRead)
def read_credits(
    user: CurrentUser, session: SessionDep, limit: PageLimit = 25, before: PageCursor = None
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
    verifier: Annotated[StoreKitVerifier, Depends(get_storekit_verifier)],
) -> CreditsRead:
    """Credits an App Store purchase to the caller and returns their credits
    after it, as GET /credits does. Safe to retry: a purchase already
    credited to the caller answers 200 and changes nothing, so the app can
    finish the transaction. A transaction that doesn't verify, isn't a
    credits product, or was refunded is a 422; one bought for another
    account (its appAccountToken), or already credited to one, is a 403 or
    409."""
    try:
        purchase = verifier.verify(payload.signed_transaction)
    except PurchaseNotVerified as exc:
        logger.warning("Refused App Store purchase from user %s: %s", user.id, exc)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if purchase.app_account_token is not None and purchase.app_account_token != user.id:
        logger.warning(
            "Refused App Store purchase %s from user %s: bought for account %s",
            purchase.external_id, user.id, purchase.app_account_token,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That purchase was made for another account")
    try:
        credited = credits.credit_purchase(
            session,
            user.id,
            external_id=purchase.external_id,
            amount_cents=purchase.amount_cents,
        )
        session.commit()
    except (PurchaseClaimed, IntegrityError):
        # IntegrityError: another account credited it at the same moment.
        session.rollback()
        logger.warning(
            "Refused App Store purchase %s from user %s: credited to another account",
            purchase.external_id, user.id,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That purchase was credited to another account"
        ) from None
    if not credited:
        response.status_code = status.HTTP_200_OK
    return read_statement(session, user.id, 25, None)
