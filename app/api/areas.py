import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, SessionDep
from app.models import Area
from app.schemas import (
    AreaCreate,
    AreaDetailRead,
    AreaPhotosRefreshRequest,
    AreaPhotosRefreshResponse,
    AreaRead,
    AreaUpdate,
)
from app.services.unsplash import (
    UnsplashClient,
    UnsplashNotConfigured,
    ensure_area_photos,
    get_unsplash_client,
)

router = APIRouter(prefix="/areas", tags=["areas"])

UnsplashDep = Annotated[UnsplashClient, Depends(get_unsplash_client)]

# Broader areas rank above narrower ones when a query matches equally well.
_KIND_RANK = case(
    (Area.kind == "country", 0),
    (Area.kind == "territory", 1),
    (Area.kind == "city", 2),
    else_=3,
)


def _get_area(session: SessionDep, area_id: uuid.UUID) -> Area:
    area = session.get(Area, area_id)
    if area is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found")
    return area


@router.post("", response_model=AreaRead, status_code=status.HTTP_201_CREATED)
def create_area(
    payload: AreaCreate, _: CurrentUser, session: SessionDep
) -> Area:
    area = Area(**payload.model_dump())
    session.add(area)
    session.commit()
    return area


@router.get("", response_model=list[AreaRead])
def list_areas(
    _: CurrentUser,
    session: SessionDep,
    q: str | None = None,
    kind: Annotated[str | None, Query(max_length=60)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Area]:
    """Text-search areas by name. Results are ranked best-match first: exact
    name, then prefix, then substring; ties broken by shorter name, then
    broader kind (country/territory before city), then alphabetically. Pass
    ``kind`` to restrict to one kind (e.g. ``city``). Without ``q`` it's a
    plain alphabetical listing."""
    query = select(Area).options(selectinload(Area.parent)).limit(limit).offset(offset)

    if kind:
        query = query.where(Area.kind == kind)

    if q:
        term = q.strip()
        match_rank = case(
            (func.lower(Area.name) == term.lower(), 0),
            (Area.name.istartswith(term, autoescape=True), 1),
            else_=2,
        )
        query = query.where(Area.name.icontains(term, autoescape=True)).order_by(
            match_rank, func.length(Area.name), _KIND_RANK, Area.name, Area.id
        )
    else:
        query = query.order_by(Area.name, Area.id)

    return list(session.scalars(query))


@router.post("/refresh-photos", response_model=AreaPhotosRefreshResponse)
def refresh_area_photos(
    payload: AreaPhotosRefreshRequest,
    _: CurrentUser,
    session: SessionDep,
    unsplash: UnsplashDep,
) -> AreaPhotosRefreshResponse:
    """Fetch cover photos for the given areas (best-effort), then return their
    current state keyed by id so the client can patch covers in.

    The second half of the two-step flow: a trip read lists the ids of its areas
    with no photos (`areas_missing_photos`); the client renders immediately, then
    calls this to fill them. Areas that already have photos are returned
    unchanged, unknown ids are omitted, and a transient failure to reach Unsplash
    (down, rate-limited) just returns the areas without photos rather than
    erroring. A missing key is the exception — it 503s, so a misconfiguration is
    obvious instead of silently looking like "no photos found"."""
    unique = list(dict.fromkeys(payload.area_ids))
    areas = list(
        session.scalars(
            select(Area)
            .options(selectinload(Area.parent), selectinload(Area.photos))
            .where(Area.id.in_(unique))
        )
    )
    try:
        ensure_area_photos(session, unsplash, areas)
    except UnsplashNotConfigured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Unsplash is not configured"
        ) from None
    return AreaPhotosRefreshResponse(areas={area.id: area for area in areas})


@router.get("/{area_id}", response_model=AreaDetailRead)
def read_area(area_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> Area:
    """A single area with its candidate photos loaded (the detail/admin view)."""
    area = session.scalar(
        select(Area)
        .options(selectinload(Area.parent), selectinload(Area.photos))
        .where(Area.id == area_id)
    )
    if area is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found")
    return area


@router.patch("/{area_id}", response_model=AreaRead)
def update_area(
    area_id: uuid.UUID, payload: AreaUpdate, _: CurrentUser, session: SessionDep
) -> Area:
    area = _get_area(session, area_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name cannot be null")
    for field, value in changes.items():
        setattr(area, field, value)
    session.commit()
    return area


@router.delete("/{area_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_area(area_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> None:
    session.delete(_get_area(session, area_id))
    session.commit()
