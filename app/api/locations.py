import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, SessionDep
from app.models import Location
from app.schemas import LocationCreate, LocationFromGoogle, LocationRead, LocationUpdate
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
    apply_google_place,
    get_google_places_client,
)

router = APIRouter(prefix="/locations", tags=["locations"])

PlacesDep = Annotated[GooglePlacesClient, Depends(get_google_places_client)]
NOT_NULLABLE = ("name",)


def _get_location(session: SessionDep, location_id: uuid.UUID) -> Location:
    location = session.get(Location, location_id)
    if location is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Location not found")
    return location


def _fetch_place(places: GooglePlacesClient, place_id: str) -> dict:
    try:
        return places.fetch_place(place_id)
    except GooglePlacesNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Google Places is not configured"
        ) from None
    except GooglePlaceNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google place not found") from None
    except GooglePlacesError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None


def _commit_unique_place(session: SessionDep) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A location with this google_place_id already exists"
        ) from None


@router.post("", response_model=LocationRead, status_code=status.HTTP_201_CREATED)
def create_location(payload: LocationCreate, user: CurrentUser, session: SessionDep) -> Location:
    location = Location(**payload.model_dump(), created_by_id=user.id)
    session.add(location)
    _commit_unique_place(session)
    return location


@router.post("/from-google", response_model=LocationRead)
def create_location_from_google(
    payload: LocationFromGoogle,
    response: Response,
    user: CurrentUser,
    session: SessionDep,
    places: PlacesDep,
) -> Location:
    """Get-or-create by Google place id: 200 if we already have it, 201 if new."""
    existing = session.scalar(
        select(Location).where(Location.google_place_id == payload.google_place_id)
    )
    if existing is not None:
        return existing

    place = _fetch_place(places, payload.google_place_id)
    location = Location(google_place_id=payload.google_place_id, created_by_id=user.id)
    apply_google_place(location, place)
    if location.name is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google returned a place with no name")
    session.add(location)
    _commit_unique_place(session)
    response.status_code = status.HTTP_201_CREATED
    return location


@router.get("", response_model=list[LocationRead])
def list_locations(
    _: CurrentUser,
    session: SessionDep,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Location]:
    query = select(Location).order_by(Location.name, Location.id).limit(limit).offset(offset)
    if q:
        query = query.where(Location.name.icontains(q, autoescape=True))
    return list(session.scalars(query))


@router.get("/{location_id}", response_model=LocationRead)
def read_location(location_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> Location:
    return _get_location(session, location_id)


@router.patch("/{location_id}", response_model=LocationRead)
def update_location(
    location_id: uuid.UUID, payload: LocationUpdate, _: CurrentUser, session: SessionDep
) -> Location:
    location = _get_location(session, location_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be null")
    if "google_place_id" in changes and changes["google_place_id"] != location.google_place_id:
        # The cache belongs to the old place.
        location.google_data = None
        location.google_fetched_at = None
    for field, value in changes.items():
        setattr(location, field, value)
    _commit_unique_place(session)
    return location


@router.post("/{location_id}/refresh-google", response_model=LocationRead)
def refresh_location_from_google(
    location_id: uuid.UUID, _: CurrentUser, session: SessionDep, places: PlacesDep
) -> Location:
    location = _get_location(session, location_id)
    if location.google_place_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Location is not linked to a Google place")
    apply_google_place(location, _fetch_place(places, location.google_place_id))
    session.commit()
    return location
