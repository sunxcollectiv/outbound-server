import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, SessionDep
from app.models import User
from app.schemas import UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, session: SessionDep) -> User:
    user = User(**payload.model_dump())
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered") from None
    return user


@router.get("/me", response_model=UserRead)
def read_me(user: CurrentUser) -> User:
    return user


@router.patch("/me", response_model=UserRead)
def update_me(payload: UserUpdate, user: CurrentUser, session: SessionDep) -> User:
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("display_name", "") is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "display_name cannot be null")
    for field, value in changes.items():
        setattr(user, field, value)
    session.commit()
    return user


@router.get("", response_model=list[UserRead])
def find_users(email: str, _: CurrentUser, session: SessionDep) -> list[User]:
    """Exact-match lookup, for adding a companion to a trip by email."""
    return list(session.scalars(select(User).where(User.email == email.lower())))


@router.get("/{user_id}", response_model=UserRead)
def read_user(user_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
