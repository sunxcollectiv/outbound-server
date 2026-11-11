import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, SessionDep
from app.models import Area
from app.schemas import (
    AdminAreaPage,
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
# Curation of the global area taxonomy. Mounted under /admin (app/api/admin.py),
# which applies the admin gate, so these resolve at /admin/areas and need no
# per-endpoint check. Client-facing reads/search stay on `router` at /areas.
admin_router = APIRouter(prefix="/areas", tags=["admin/areas"])

UnsplashDep = Annotated[UnsplashClient, Depends(get_unsplash_client)]

# Broader areas rank above narrower ones when a query matches equally well.
_KIND_RANK = case(
    (Area.kind == "country", 0),
    (Area.kind == "territory", 1),
    (Area.kind.in_(("region", "island")), 2),
    (Area.kind == "city", 3),
    else_=4,
)


def _get_area(session: SessionDep, area_id: uuid.UUID) -> Area:
    area = session.get(Area, area_id)
    if area is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found")
    return area


@admin_router.post(
    "",
    response_model=AreaRead,
    status_code=status.HTTP_201_CREATED,
    summary="[admin] Create area",
)
def create_area(payload: AreaCreate, session: SessionDep) -> Area:
    """Create an area. The taxonomy is free-form: `kind` (country/territory/
    city/…) plus a numeric `level`, with an optional `parent_id` for nesting."""
    area = Area(**payload.model_dump())
    session.add(area)
    session.commit()
    return area


def _area_search(
    q: str | None, kind: str | None
) -> tuple[list[ColumnElement[bool]], list[ColumnElement]]:
    """The filters and ordering shared by the client search and the admin list.
    With a (non-blank) ``q``: name substring match, ranked exact name, then
    prefix, then substring; ties broken by shorter name, then broader kind
    (country/territory, then region/island, then city), then alphabetically.
    Without it: plain alphabetical. ``kind`` restricts to one kind (e.g.
    ``city``)."""
    filters: list[ColumnElement[bool]] = []
    if kind:
        filters.append(Area.kind == kind)
    if q and (term := q.strip()):
        filters.append(Area.name.icontains(term, autoescape=True))
        match_rank = case(
            (func.lower(Area.name) == term.lower(), 0),
            (Area.name.istartswith(term, autoescape=True), 1),
            else_=2,
        )
        return filters, [match_rank, func.length(Area.name), _KIND_RANK, Area.name, Area.id]
    return filters, [Area.name, Area.id]


def _search_areas(
    session: Session, q: str | None, kind: str | None, limit: int, offset: int
) -> list[Area]:
    filters, order_by = _area_search(q, kind)
    return list(
        session.scalars(
            select(Area)
            .options(selectinload(Area.parent))
            .where(*filters)
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
    )


def _read_area_detail(session: Session, area_id: uuid.UUID) -> Area:
    area = session.scalar(
        select(Area)
        .options(selectinload(Area.parent), selectinload(Area.photos))
        .where(Area.id == area_id)
    )
    if area is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found")
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
    broader kind (country/territory, then region/island, then city), then
    alphabetically. Pass ``kind`` to restrict to one kind (e.g. ``city``).
    Without ``q`` it's a plain alphabetical listing."""
    return _search_areas(session, q, kind, limit, offset)


@admin_router.get("", response_model=AdminAreaPage, summary="[admin] List areas")
def admin_list_areas(
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="filter by name (case-insensitive substring), ranked best match first"),
    ] = None,
    kind: Annotated[str | None, Query(max_length=60, description="restrict to one kind")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminAreaPage:
    """Browse the area taxonomy for the management dashboard. Same filters and
    ordering as the client search (`GET /areas`); `total` is the count across
    all pages, for the pager."""
    filters, _order = _area_search(q, kind)
    total = session.scalar(select(func.count()).select_from(Area).where(*filters)) or 0
    items = _search_areas(session, q, kind, limit, offset)
    return AdminAreaPage(
        items=[AreaRead.model_validate(area) for area in items],
        total=total,
        limit=limit,
        offset=offset,
    )


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
    """A single area with its candidate photos loaded (the detail view)."""
    return _read_area_detail(session, area_id)


@admin_router.get("/{area_id}", response_model=AreaDetailRead, summary="[admin] Read area")
def admin_read_area(area_id: uuid.UUID, session: SessionDep) -> Area:
    """A single area with its parent and candidate photos, for the dashboard's
    detail/edit view."""
    return _read_area_detail(session, area_id)


@admin_router.patch("/{area_id}", response_model=AreaRead, summary="[admin] Update area")
def update_area(
    area_id: uuid.UUID, payload: AreaUpdate, session: SessionDep
) -> Area:
    """Edit an area's fields (only those sent are changed)."""
    area = _get_area(session, area_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "name cannot be null")
    for field, value in changes.items():
        setattr(area, field, value)
    session.commit()
    return area


@admin_router.delete(
    "/{area_id}", status_code=status.HTTP_204_NO_CONTENT, summary="[admin] Delete area"
)
def delete_area(area_id: uuid.UUID, session: SessionDep) -> None:
    """Delete an area. Trips that referenced it simply drop the id on their
    next read (area_ids holds no foreign key)."""
    session.delete(_get_area(session, area_id))
    session.commit()
