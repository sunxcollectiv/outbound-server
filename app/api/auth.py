from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentAccessToken, SessionDep
from app.config import get_settings
from app.models import AccessToken, TripMember, User
from app.schemas import Credentials, TokenRead, UserCreate
from app.security import hash_password, hash_token, new_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token(session: Session, user: User) -> TokenRead:
    token = new_token()
    access_token = AccessToken(
        user=user,
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().access_token_ttl_days),
    )
    session.add(access_token)
    session.commit()
    return TokenRead(token=token, expires_at=access_token.expires_at, user=user)


@router.post("/register", response_model=TokenRead, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, session: SessionDep) -> TokenRead:
    """Also links the new account to any trips they were added to by email, so
    those trips are theirs to open straight away."""
    fields = payload.model_dump(exclude={"password"})
    user = User(**fields, password_hash=hash_password(payload.password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered") from None
    waiting = select(TripMember).where(
        TripMember.email == user.email, TripMember.user_id.is_(None)
    )
    for member in session.scalars(waiting):
        member.user = user
    return _issue_token(session, user)


@router.post("/login", response_model=TokenRead)
def login(payload: Credentials, session: SessionDep) -> TokenRead:
    user = session.scalar(select(User).where(User.email == payload.email))
    if not verify_password(payload.password, user.password_hash if user else None):
        # Same answer for an unknown email and a wrong password.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return _issue_token(session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(access_token: CurrentAccessToken, session: SessionDep) -> None:
    session.delete(access_token)
    session.commit()
