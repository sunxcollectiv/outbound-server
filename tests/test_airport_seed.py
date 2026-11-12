import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Area, Place
from app.seeds._airport_google_ids import GOOGLE_PLACE_IDS
from app.seeds._airports_data import AIRPORTS
from app.seeds.airports import _city_candidates, has_seeded_address, seed_airports
from app.services.google_places import apply_google_place
from app.seeds.cities import seed_cities
from app.seeds.countries import seed_areas


def _airport(session: Session, code: str) -> Place:
    return session.scalar(select(Place).where(Place.category == "airport", Place.code == code))


def test_airport_data_is_well_formed():
    codes = [code for code, *_ in AIRPORTS]
    assert len(set(codes)) == len(codes), "each IATA code appears once"
    assert all(re.fullmatch(r"[A-Z0-9]{3}", code) for code in codes)
    assert {"LAX", "HND", "LHR", "CDG", "SIN"} <= set(codes)

    for code, name, _city, _country, latitude, longitude, timezone in AIRPORTS:
        assert name and len(name) <= 200, code
        assert -90 <= latitude <= 90 and -180 <= longitude <= 180, code
        ZoneInfo(timezone)  # raises on a zone our tzdata doesn't know


def test_seed_airports_creates_places_linked_to_their_city(session: Session):
    seed_areas(session)
    seed_cities(session)
    result = seed_airports(session)

    assert result["created"] == len(AIRPORTS)
    assert result["skipped"] == 0
    assert 0 < result["linked"] < result["created"]

    lax = _airport(session, "LAX")
    assert lax.name == "Los Angeles International Airport"
    assert lax.address == "Los Angeles, United States"
    assert lax.timezone == "America/Los_Angeles"
    assert round(lax.latitude, 2) == 33.94
    assert lax.google_place_id == GOOGLE_PLACE_IDS["LAX"]
    assert lax.google_data is None  # ids only; details are fetched later
    assert lax.area.name == "Los Angeles"
    assert lax.area.parent.name == "United States"

    # A qualified municipality ("Paris (Roissy-en-France, Val-d'Oise)") still
    # finds its city.
    assert _airport(session, "CDG").area.name == "Paris"


def test_seed_airports_without_areas_leaves_area_empty(session: Session):
    result = seed_airports(session)
    assert result["created"] == len(AIRPORTS)
    assert result["linked"] == 0
    assert _airport(session, "LAX").area_id is None


def test_seed_airports_is_idempotent_and_keeps_edits(session: Session):
    seed_airports(session)
    lax = _airport(session, "LAX")
    lax.name = "LAX (renamed by an admin)"
    session.commit()

    second = seed_airports(session)
    assert second["created"] == 0
    assert second["skipped"] == len(AIRPORTS)

    session.expire_all()
    assert _airport(session, "LAX").name == "LAX (renamed by an admin)"
    (count,) = session.execute(
        select(func.count()).select_from(Place).where(Place.code == "LAX")
    ).one()
    assert count == 1


def test_seed_airports_skips_a_code_that_already_exists(session: Session):
    """An airport an import already created (with its code) is left as it is."""
    session.add(Place(name="Haneda", category="airport", code="HND"))
    session.commit()

    result = seed_airports(session)
    assert result["created"] == len(AIRPORTS) - 1
    assert _airport(session, "HND").name == "Haneda"


def test_same_code_in_another_category_is_not_an_airport(session: Session):
    session.add(Place(name="Some station", category="train_station", code="LAX"))
    session.commit()

    result = seed_airports(session)
    assert result["created"] == len(AIRPORTS)
    assert _airport(session, "LAX").name == "Los Angeles International Airport"


def test_city_candidates_strip_qualifiers():
    paris = _city_candidates("Paris (Roissy-en-France, Val-d'Oise)")
    assert paris[0] == "Paris (Roissy-en-France, Val-d'Oise)"  # as written, first
    assert "Paris" in paris
    assert _city_candidates("Kuta, Badung") == ["Kuta, Badung", "Kuta"]
    assert _city_candidates("Istanbul(Bakırköy)") == ["Istanbul(Bakırköy)", "Istanbul"]
    assert _city_candidates("Tokyo") == ["Tokyo"]


def test_google_id_list_only_names_seeded_airports():
    codes = {code for code, *_ in AIRPORTS}
    assert set(GOOGLE_PLACE_IDS) <= codes
    assert len(set(GOOGLE_PLACE_IDS.values())) == len(GOOGLE_PLACE_IDS), "one airport per id"
    assert all(value.startswith("ChIJ") for value in GOOGLE_PLACE_IDS.values())


def test_seed_applies_google_ids_to_new_and_existing_airports(session: Session):
    session.add(Place(name="Haneda", category="airport", code="HND"))
    session.commit()

    result = seed_airports(session)

    assert result["google_ids"] == len(GOOGLE_PLACE_IDS)
    assert result["google_id_conflicts"] == 0
    # The airport that was already there gets its id; its own name is kept.
    hnd = _airport(session, "HND")
    assert (hnd.name, hnd.google_place_id) == ("Haneda", GOOGLE_PLACE_IDS["HND"])

    second = seed_airports(session)
    assert (second["created"], second["google_ids"]) == (0, 0)


def test_seed_skips_a_google_id_another_place_holds(session: Session):
    """A place a user added from Google before the seed keeps its id; the
    seeded airport goes without, for the link script to merge."""
    session.add(Place(name="LAX", google_place_id=GOOGLE_PLACE_IDS["LAX"]))
    session.commit()

    result = seed_airports(session)

    assert result["google_id_conflicts"] == 1
    assert result["google_ids"] == len(GOOGLE_PLACE_IDS) - 1
    assert _airport(session, "LAX").google_place_id is None


def test_first_google_fetch_replaces_the_placeholder_address(session: Session):
    seed_airports(session)
    lax = _airport(session, "LAX")
    assert has_seeded_address(lax)

    apply_google_place(lax, {"formattedAddress": "1 World Way, Los Angeles, CA 90045, USA"})
    assert lax.address == "1 World Way, Los Angeles, CA 90045, USA"
    assert not has_seeded_address(lax)

    # An address someone typed is never replaced.
    jfk = _airport(session, "JFK")
    jfk.address = "Queens, NY"
    apply_google_place(jfk, {"formattedAddress": "Queens, NY 11430, USA"})
    assert jfk.address == "Queens, NY"

    # Nor is a non-airport's address that happens to look like a placeholder.
    other = Place(name="Office", address="Los Angeles, United States")
    apply_google_place(other, {"formattedAddress": "1 Main St"})
    assert other.address == "Los Angeles, United States"
