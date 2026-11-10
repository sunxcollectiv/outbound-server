from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Area
from app.seeds._regions_data import REGIONS
from app.seeds.cities import seed_cities
from app.seeds.countries import seed_areas
from app.seeds.regions import seed_regions


def _country(session: Session, name: str) -> Area:
    return session.scalar(select(Area).where(Area.name == name, Area.kind == "country"))


def test_seed_regions_links_to_parent_country(session: Session):
    seed_areas(session)  # parents
    result = seed_regions(session)

    assert result["unresolved"] == 0, "every region should resolve a parent country"
    assert result["created"] == len(REGIONS)

    bali = session.scalar(select(Area).where(Area.name == "Bali"))
    assert bali.kind == "island"
    assert bali.parent.name == "Indonesia"

    tuscany = session.scalar(select(Area).where(Area.name == "Tuscany"))
    assert tuscany.kind == "region"
    assert tuscany.parent.name == "Italy"

    # A territory can be the parent too.
    tahiti = session.scalar(select(Area).where(Area.name == "Tahiti"))
    assert tahiti.parent.name == "French Polynesia"
    assert tahiti.parent.kind == "territory"


def test_region_kinds_are_region_or_island():
    assert {kind for _, kind, _ in REGIONS} == {"region", "island"}


def test_seed_regions_is_idempotent(session: Session):
    seed_areas(session)
    seed_regions(session)
    second = seed_regions(session)
    assert second["created"] == 0
    assert second["skipped"] == len(REGIONS)

    (count,) = session.execute(
        select(func.count()).select_from(Area).where(Area.name == "Bali")
    ).one()
    assert count == 1


def test_seed_regions_skips_a_name_already_under_the_same_parent(session: Session):
    """A place that already exists under the country, in any kind and ignoring
    accents, is left alone rather than duplicated."""
    seed_areas(session)
    session.add(Area(name="Crete", kind="city", parent_id=_country(session, "Greece").id))
    session.add(Area(name="Roatan", kind="city", parent_id=_country(session, "Honduras").id))
    session.commit()

    result = seed_regions(session)
    assert result["created"] == len(REGIONS) - 2
    assert result["skipped"] == 2

    crete = session.scalars(select(Area).where(Area.name == "Crete")).all()
    assert [a.kind for a in crete] == ["city"]
    assert session.scalar(select(Area).where(Area.name == "Roatán")) is None


def test_regions_do_not_repeat_seeded_cities(session: Session):
    """Guards the data: nothing in REGIONS is already a seeded city under the
    same country, so every entry is really added on a fully seeded database."""
    seed_areas(session)
    seed_cities(session)
    result = seed_regions(session)
    assert result["created"] == len(REGIONS)


def test_seed_regions_skips_when_parent_missing(session: Session):
    # No countries seeded: every region is unresolved, nothing inserted.
    result = seed_regions(session)
    assert result["created"] == 0
    assert result["unresolved"] == len(REGIONS)
