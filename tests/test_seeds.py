from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Area
from app.seeds.countries import COUNTRIES, TERRITORIES, seed_areas


def test_seed_areas_inserts_full_list(session: Session):
    result = seed_areas(session)
    assert result["created"] == len(COUNTRIES) + len(TERRITORIES)
    assert result["skipped"] == 0

    countries = set(
        session.scalars(select(Area.name).where(Area.kind == "country"))
    )
    # Taiwan is its own country; China uses the common name.
    assert {"Taiwan", "China", "South Korea", "Vatican City"} <= countries

    hong_kong = session.scalar(select(Area).where(Area.name == "Hong Kong"))
    assert hong_kong.kind == "territory"


def test_seed_areas_is_idempotent(session: Session):
    seed_areas(session)
    second = seed_areas(session)
    assert second["created"] == 0
    assert second["skipped"] == len(COUNTRIES) + len(TERRITORIES)

    # No duplicate country rows after two runs.
    (japan_count,) = session.execute(
        select(func.count()).select_from(Area).where(Area.name == "Japan")
    ).one()
    assert japan_count == 1


def test_seed_areas_coexists_with_a_same_named_city(session: Session):
    # A derived city area (kind NULL) must not block seeding the country.
    session.add(Area(name="Singapore"))
    session.flush()

    seed_areas(session)

    kinds = set(
        session.scalars(select(Area.kind).where(Area.name == "Singapore"))
    )
    assert kinds == {None, "country"}
