import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AccessToken, Trip, TripMember, TripRole, User
from app.security import hash_token

SessionDep = Annotated[Session, Depends(get_session)]

# auto_error=False so get_access_token decides what a missing header returns.
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def get_access_token(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AccessToken:
    if credentials is None:
        raise _unauthorized("Not authenticated")
    access_token = session.scalar(
        select(AccessToken).where(
            AccessToken.token_hash == hash_token(credentials.credentials),
            AccessToken.expires_at > datetime.now(UTC),
        )
    )
    if access_token is None:
        raise _unauthorized("Invalid or expired token")
    return access_token


CurrentAccessToken = Annotated[AccessToken, Depends(get_access_token)]


def get_current_user(access_token: CurrentAccessToken) -> User:
    return access_token.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def _trip_role(session: Session, trip: Trip, user: User) -> TripRole | None:
    return session.scalar(
        select(TripMember.role).where(
            TripMember.trip_id == trip.id, TripMember.user_id == user.id
        )
    )


def _load_trip(session: Session, trip_id: uuid.UUID, user: User) -> tuple[Trip, bool, bool]:
    """Returns (trip, is_owner, can_edit). 404s if the user can't see the trip,
    so trip ids don't leak to people who aren't on them."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    if trip.owner_id == user.id:
        return trip, True, True
    role = _trip_role(session, trip, user)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")
    return trip, False, role is TripRole.EDITOR


def get_viewable_trip(trip_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Trip:
    trip, _, _ = _load_trip(session, trip_id, user)
    return trip


def get_editable_trip(trip_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Trip:
    trip, _, can_edit = _load_trip(session, trip_id, user)
    if not can_edit:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only view this trip")
    return trip


def get_owned_trip(trip_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Trip:
    trip, is_owner, _ = _load_trip(session, trip_id, user)
    if not is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the trip owner can do this")
    return trip


ViewableTrip = Annotated[Trip, Depends(get_viewable_trip)]
EditableTrip = Annotated[Trip, Depends(get_editable_trip)]
OwnedTrip = Annotated[Trip, Depends(get_owned_trip)]
