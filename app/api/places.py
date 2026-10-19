import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, SessionDep
from app.models import Area, Place
from app.schemas import (
    PlaceCreate,
    PlaceRead,
    PlacesRefreshRequest,
    PlacesRefreshResponse,
    PlaceUpdate,
)
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
    apply_area_from_google,
    apply_google_place,
    get_google_places_client,
    place_needs_refresh,
)

router = APIRouter(prefix="/places", tags=["places"])

PlacesDep = Annotated[GooglePlacesClient, Depends(get_google_places_client)]
NOT_NULLABLE = ("name",)


def _get_place(session: SessionDep, place_id: uuid.UUID) -> Place:
    place = session.get(Place, place_id)
    if place is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Place not found")
    return place


def _check_area(session: SessionDep, area_id: uuid.UUID | None) -> None:
    if area_id is not None and session.get(Area, area_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "area_id does not match a area"
        )


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
            status.HTTP_409_CONFLICT, "A place with this google_place_id already exists"
        ) from None


def _try_enrich(session: SessionDep, places: GooglePlacesClient, place: Place) -> None:
    """Best-effort enrichment for a freshly created place. A bare stub is an
    acceptable outcome when Google can't answer (unknown id, not configured,
    network), so any failure is swallowed and the place is left to be refreshed
    later rather than failing the create."""
    try:
        details = places.fetch_place(place.google_place_id)
    except GooglePlacesError:
        session.rollback()  # nothing applied yet; just drop the empty transaction
        return
    apply_google_place(place, details)
    apply_area_from_google(session, place, details)
    session.commit()


@router.post("", response_model=PlaceRead, status_code=status.HTTP_201_CREATED)
def create_place(
    payload: PlaceCreate,
    response: Response,
    user: CurrentUser,
    session: SessionDep,
    places: PlacesDep,
) -> Place:
    """Register a Google place and enrich it from Google Place Details in the
    same request. Get-or-create on the unique google_place_id: 200 if we already
    have it (returned as-is, no re-fetch), 201 if new. A new place is enriched
    best-effort — if Google can't answer it is left as a bare stub for a later
    refresh, rather than failing the create."""
    existing = session.scalar(
        select(Place).where(Place.google_place_id == payload.google_place_id)
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    place = Place(google_place_id=payload.google_place_id, created_by_id=user.id)
    session.add(place)
    _commit_unique_place(session)
    _try_enrich(session, places, place)
    return place


@router.post("/refresh", response_model=PlacesRefreshResponse)
def refresh_places(
    payload: PlacesRefreshRequest, _: CurrentUser, session: SessionDep, places: PlacesDep
) -> PlacesRefreshResponse:
    """Batch, TTL-gated refresh for the places the itinerary flagged stale. Only
    places still due a refresh are re-fetched from Google (best-effort — a
    failure leaves the cached data), so two clients opening the same trip don't
    double-fetch. `refresh_after_days` overrides the staleness threshold (0 forces
    every id). Returns the current state of every requested-and-known place,
    keyed by id, for the client to patch its cards by."""
    found = session.scalars(select(Place).where(Place.id.in_(payload.place_ids))).all()
    for place in found:
        if place.google_place_id is not None and place_needs_refresh(
            place, after_days=payload.refresh_after_days
        ):
            _try_enrich(session, places, place)
    return PlacesRefreshResponse(places={place.id: PlaceRead.model_validate(place) for place in found})


@router.get("", response_model=list[PlaceRead])
def list_places(
    _: CurrentUser,
    session: SessionDep,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Place]:
    query = (
        select(Place)
        .options(selectinload(Place.area))
        .order_by(Place.name, Place.id)
        .limit(limit)
        .offset(offset)
    )
    if q:
        query = query.where(Place.name.icontains(q, autoescape=True))
    return list(session.scalars(query))


@router.get("/{place_id}", response_model=PlaceRead)
def read_place(place_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> Place:
    return _get_place(session, place_id)


@router.patch("/{place_id}", response_model=PlaceRead)
def update_place(
    place_id: uuid.UUID, payload: PlaceUpdate, _: CurrentUser, session: SessionDep
) -> Place:
    place = _get_place(session, place_id)
    changes = payload.model_dump(exclude_unset=True)
    for field in NOT_NULLABLE:
        if field in changes and changes[field] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{field} cannot be null")
    if "area_id" in changes:
        _check_area(session, changes["area_id"])
    if "google_place_id" in changes and changes["google_place_id"] != place.google_place_id:
        # The cache belongs to the old place.
        place.google_data = None
        place.google_fetched_at = None
    for field, value in changes.items():
        setattr(place, field, value)
    _commit_unique_place(session)
    return place


@router.post("/{place_id}/refresh-google", response_model=PlaceRead)
def refresh_place_from_google(
    place_id: uuid.UUID, _: CurrentUser, session: SessionDep, places: PlacesDep
) -> Place:
    place = _get_place(session, place_id)
    if place.google_place_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Place is not linked to a Google place")
    details = _fetch_place(places, place.google_place_id)
    apply_google_place(place, details)
    apply_area_from_google(session, place, details)
    session.commit()
    return place
