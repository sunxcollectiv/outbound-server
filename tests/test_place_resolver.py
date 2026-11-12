from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Place
from app.services.place_resolver import resolve_place


def test_resolves_by_code_only_when_the_caller_says_it_is_an_airport(session: Session):
    lax = Place(name="Los Angeles International Airport", category="airport", code="LAX")
    session.add(lax)
    session.flush()

    by_code = resolve_place(session, {"name": "LA Intl", "iata_code": " lax "}, code_category="airport")
    assert by_code == lax.id

    # A station code from a train booking must not land on the airport.
    station = resolve_place(session, {"name": "LAX", "iata_code": "LAX"})
    assert station != lax.id
    assert session.get(Place, station).code is None


def test_creates_a_missing_airport_with_its_code_and_zone(session: Session):
    place_id = resolve_place(
        session,
        {"name": None, "iata_code": "ITM"},
        timezone="Asia/Tokyo",
        code_category="airport",
    )
    place = session.get(Place, place_id)
    assert (place.name, place.category, place.code, place.timezone) == ("ITM", "airport", "ITM", "Asia/Tokyo")

    again = resolve_place(session, {"name": "Osaka Itami", "iata_code": "ITM"}, code_category="airport")
    assert again == place_id


def test_an_unusable_code_falls_back_to_name_and_address(session: Session):
    for bad in ("", "LAXX", "L-X", None):
        place_id = resolve_place(
            session, {"name": "Some Airfield", "iata_code": bad}, code_category="airport"
        )
        assert session.get(Place, place_id).code is None


def test_cache_is_shared_between_code_and_name_matches(session: Session):
    cache: dict = {}
    first = resolve_place(session, {"iata_code": "HND"}, code_category="airport", cache=cache)
    second = resolve_place(session, {"name": "Haneda", "iata_code": "HND"}, code_category="airport", cache=cache)
    assert first == second
    assert len(session.scalars(select(Place).where(Place.code == "HND")).all()) == 1
