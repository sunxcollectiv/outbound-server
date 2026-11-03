from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas import CreditsRead
from app.services import credits

router = APIRouter(prefix="/credits", tags=["credits"])


@router.get("", response_model=CreditsRead)
def read_credits(user: CurrentUser, session: SessionDep) -> CreditsRead:
    """The caller's balance and every movement on it, newest first."""
    return CreditsRead(
        balance_cents=credits.balance(session, user.id),
        entries=credits.ledger(session, user.id),
    )
