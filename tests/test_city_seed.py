from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Area
from app.seeds._cities_data import CITIES
from app.seeds.cities import seed_cities
from app.seeds.countries import seed_areas


def test_seed_cities_links_to_parent_country(session: Session):
    seed_areas(session)  # parents
    result = seed_cities(session)

    assert result["unresolved"] == 0, "every city should resolve a parent country"
    assert result["created"] == len(CITIES)

    # A city points at its country via parent_id.
    kyoto = session.scalar(select(Area).where(Area.name == "Kyoto", Area.kind == "city"))
    assert kyoto.parent is not None
    assert kyoto.parent.name == "Japan"
    assert kyoto.parent.kind == "country"


def test_same_name_cities_in_different_countries_coexist(session: Session):
    seed_areas(session)
    seed_cities(session)

    cordobas = session.scalars(
        select(Area).where(Area.name == "Córdoba", Area.kind == "city")
    ).all()
    parent_names = {c.parent.name for c in cordobas}
    assert {"Spain", "Argentina"} <= parent_names


def test_seed_cities_is_idempotent(session: Session):
    seed_areas(session)
    seed_cities(session)
    second = seed_cities(session)
    assert second["created"] == 0

    (count,) = session.execute(
        select(func.count())
        .select_from(Area)
        .where(Area.name == "Kyoto", Area.kind == "city")
    ).one()
    assert count == 1


def test_seed_cities_skips_when_parent_missing(session: Session):
    # No countries seeded: every city is unresolved, nothing inserted.
    result = seed_cities(session)
    assert result["created"] == 0
    assert result["unresolved"] == len(CITIES)
