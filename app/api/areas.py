import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.models import Area
from app.schemas import AreaCreate, AreaRead, AreaUpdate

router = APIRouter(prefix="/areas", tags=["areas"])


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
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Area]:
    query = (
        select(Area).order_by(Area.name, Area.id).limit(limit).offset(offset)
    )
    if q:
        query = query.where(Area.name.icontains(q, autoescape=True))
    return list(session.scalars(query))


@router.get("/{area_id}", response_model=AreaRead)
def read_area(area_id: uuid.UUID, _: CurrentUser, session: SessionDep) -> Area:
    return _get_area(session, area_id)


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
