import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.models import CreditEntry
from app.schemas import CreditsRead
from app.services import credits

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
