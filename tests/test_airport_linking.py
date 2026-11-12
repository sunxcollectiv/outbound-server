import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import Place, TripDay
from app.services.airport_linking import (
    choose_match,
    distance_km,
    link_airport,
    merge_places,
    names_agree,
)
from app.services.google_places import (
    SEARCH_FIELD_MASK,
    TEXT_SEARCH_URL,
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
)

LAX = {"latitude": 33.9425, "longitude": -118.408}


def _lax(session: Session, **overrides) -> Place:
    values = dict(
        name="Los Angeles International Airport",
        category="airport",
        code="LAX",
        address="Los Angeles, United States",
        timezone="America/Los_Angeles",
        **LAX,
    )
    values.update(overrides)
    place = Place(**values)
    session.add(place)
    session.flush()
    return place


def _candidate(
    place_id: str = "ChIJ-lax",
    name: str = "Los Angeles International Airport",
    *,
    latitude: float = 33.9416,
    longitude: float = -118.4085,
    primary_type: str | None = "international_airport",
    types: list[str] | None = None,
) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": name},
        "formattedAddress": "1 World Way, Los Angeles, CA 90045, USA",
        "location": {"latitude": latitude, "longitude": longitude},
        "timeZone": {"id": "America/Los_Angeles"},
        "primaryType": primary_type,
        "types": types if types is not None else ["international_airport", "airport"],
        "photos": [{"name": "places/ChIJ-lax/photos/abc", "authorAttributions": []}],
    }


def test_names_agree_on_shared_distinctive_words():
    assert names_agree("Los Angeles International Airport", "Los Angeles International Airport")
    assert names_agree("Cancún International Airport", "Aeropuerto Internacional de Cancun")
    assert names_agree(
        "Denpasar I Gusti Ngurah Rai International Airport",
        "I Gusti Ngurah Rai International Airport",
    )
    # Google's name carrying our code is enough on its own.
    assert names_agree("Suvarnabhumi Airport", "Bangkok Airport (BKK)", "BKK")


def test_names_disagree_when_only_generic_words_are_shared():
    assert not names_agree("Tokyo Haneda International Airport", "Narita International Airport")
    assert not names_agree("Hollywood Burbank Airport", "Van Nuys Airport")
    assert not names_agree("International Airport", "Regional Airport")


def test_distance_km():
    assert distance_km(33.9425, -118.408, 33.9425, -118.408) == 0
    # LAX to Burbank is about 29 km.
    assert 27 < distance_km(33.9425, -118.408, 34.2007, -118.3587) < 31


def test_links_a_confident_match_and_takes_the_real_address(session, google_places):
    place = _lax(session)
    google_places.search_results = [_candidate()]

    result = link_airport(session, google_places, place)

    assert result.linked
    assert result.google_place_id == "ChIJ-lax"
    assert place.google_place_id == "ChIJ-lax"
    assert place.address == "1 World Way, Los Angeles, CA 90045, USA"
    assert place.google_data["photos"]  # cached, so the cover photo works
    assert place.google_fetched_at is not None
    # Ours are kept: Google never overwrites the name, category or coordinates.
    assert place.name == "Los Angeles International Airport"
    assert place.category == "airport"
    assert place.latitude == LAX["latitude"]

    search = google_places.searches[0]
    assert search["query"] == "Los Angeles International Airport LAX"
    assert search["latitude"] == LAX["latitude"]
    assert search["included_type"] == "airport"


def test_keeps_an_address_someone_edited(session, google_places):
    place = _lax(session, address="World Way, Westchester")
    google_places.search_results = [_candidate()]

    assert link_airport(session, google_places, place).linked
    assert place.address == "World Way, Westchester"


def test_does_not_link_a_far_away_airport(session, google_places):
    place = _lax(session)
    # Same name, but 29 km away (Burbank's coordinates).
    google_places.search_results = [_candidate(latitude=34.2007, longitude=-118.3587)]

    result = link_airport(session, google_places, place)

    assert not result.linked
    assert "no airport within" in result.reason
    assert place.google_place_id is None
    assert place.address == "Los Angeles, United States"


def test_does_not_link_something_that_is_not_an_airport(session, google_places):
    place = _lax(session)
    google_places.search_results = [
        _candidate(name="Los Angeles Airport Marriott", primary_type="hotel", types=["hotel", "lodging"])
    ]
    assert not link_airport(session, google_places, place).linked


def test_takes_the_top_result_when_duplicates_share_the_spot(session, google_places):
    """Google lists one airport more than once; same spot means same airport."""
    place = _lax(session)
    google_places.search_results = [
        _candidate("ChIJ-a"),
        _candidate("ChIJ-b", "LAX", latitude=33.9440, longitude=-118.4070),
    ]

    result = link_airport(session, google_places, place)

    assert result.linked
    assert result.google_place_id == "ChIJ-a"
    assert "2 listings at the same spot" in result.reason


def test_does_not_link_when_two_fitting_results_are_apart(session, google_places):
    place = _lax(session)
    google_places.search_results = [
        _candidate("ChIJ-a"),
        # Same name, 3 km north: within range of us, but not the same spot.
        _candidate("ChIJ-b", "Los Angeles International Airport Annex", latitude=33.9690),
    ]

    result = link_airport(session, google_places, place)

    assert not result.linked
    assert result.reason.startswith("ambiguous")
    assert place.google_place_id is None


def test_picks_the_named_airport_over_a_neighbour(session, google_places):
    place = _lax(session)
    google_places.search_results = [
        _candidate("ChIJ-other", "Hawthorne Municipal Airport", latitude=33.95, longitude=-118.40),
        _candidate("ChIJ-lax"),
    ]
    assert link_airport(session, google_places, place).google_place_id == "ChIJ-lax"


def test_a_differently_named_airport_links_only_when_alone_on_the_spot(session, google_places):
    place = _lax(session)
    renamed = _candidate(name="Tom Bradley Field")  # ~0.1 km away
    google_places.search_results = [renamed]
    result = link_airport(session, google_places, place)
    assert result.linked and "name differs" in result.reason

    # The same result 3 km off is within range but not "on the spot".
    other = _lax(session, code="LAY")
    google_places.search_results = [_candidate("ChIJ-far", "Tom Bradley Field", latitude=33.9695)]
    assert not link_airport(session, google_places, other).linked


def test_merges_into_the_place_that_already_has_the_google_id(
    session, google_places, client, make_user, make_trip
):
    """An airport added through the app before the seed keeps its row (and the
    trips pointing at it) and takes the code; the seeded duplicate goes."""
    keeper = Place(
        name="LAX",
        category="airport",
        address="1 World Way, Los Angeles",
        google_place_id="ChIJ-lax",
    )
    session.add(keeper)
    place = _lax(session)
    seeded_id = place.id
    trip = make_trip(make_user("sun"))
    day = TripDay(trip_id=uuid.UUID(trip["id"]), date=date(2030, 1, 1), place_id=seeded_id)
    session.add(day)
    session.flush()
    google_places.search_results = [_candidate()]

    result = link_airport(session, google_places, place)
    session.expire_all()

    assert result.linked
    assert result.merged_into == keeper.id
    assert session.get(Place, seeded_id) is None
    assert (keeper.category, keeper.code) == ("airport", "LAX")
    # The keeper's own values win; the seed fills what it lacked.
    assert keeper.name == "LAX"
    assert keeper.address == "1 World Way, Los Angeles"
    assert keeper.timezone == "America/Los_Angeles"
    assert keeper.latitude == LAX["latitude"]
    # What pointed at the seeded row now points at the keeper.
    assert day.place_id == keeper.id


def test_does_not_merge_into_a_place_with_a_different_code(session, google_places):
    existing = Place(name="Other", category="airport", code="LGB", google_place_id="ChIJ-lax")
    session.add(existing)
    place = _lax(session)
    google_places.search_results = [_candidate()]

    result = link_airport(session, google_places, place)

    assert not result.linked
    assert "LGB" in result.reason
    assert place.google_place_id is None
    assert session.get(Place, place.id) is not None


def test_no_results_and_no_coordinates(session, google_places):
    place = _lax(session)
    assert not link_airport(session, google_places, place).linked

    assert choose_match(Place(name="X", code="XXX"), [_candidate()])[0] is None


def test_search_text_request_and_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["mask"] = request.headers["X-Goog-FieldMask"]
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"places": [{"id": "ChIJ-lax"}]})

    client = GooglePlacesClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    results = client.search_text("LAX", latitude=1.0, longitude=2.0, included_type="airport")

    assert results == [{"id": "ChIJ-lax"}]
    assert seen["url"] == TEXT_SEARCH_URL
    assert seen["mask"] == SEARCH_FIELD_MASK
    assert seen["mask"].startswith("places.id,places.displayName")
    assert seen["body"]["textQuery"] == "LAX"
    assert seen["body"]["includedType"] == "airport"
    assert seen["body"]["locationBias"]["circle"]["center"] == {"latitude": 1.0, "longitude": 2.0}


def test_search_text_errors():
    with pytest.raises(GooglePlacesNotConfigured):
        GooglePlacesClient(None).search_text("LAX")

    empty = GooglePlacesClient(
        "key", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    )
    assert empty.search_text("nowhere") == []

    broken = GooglePlacesClient(
        "key", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    )
    with pytest.raises(GooglePlacesError):
        broken.search_text("LAX")


def test_merge_places_folds_a_codeless_stub_into_the_airport(
    session, client, make_user, make_trip
):
    """An import-made stub (name only) merges into the real airport: the airport
    keeps its code and data, and the trip that used the stub now uses it."""
    airport = _lax(session, google_place_id="ChIJ-lax")
    stub = Place(name="Los Angeles Intl", notes=None)
    session.add(stub)
    session.flush()
    stub_id = stub.id
    trip = make_trip(make_user("sun"))
    day = TripDay(trip_id=uuid.UUID(trip["id"]), date=date(2030, 1, 1), place_id=stub_id)
    session.add(day)
    session.flush()

    merge_places(session, stub, airport)
    session.expire_all()

    assert session.get(Place, stub_id) is None
    assert day.place_id == airport.id
    assert (airport.category, airport.code) == ("airport", "LAX")
    assert airport.name == "Los Angeles International Airport"
    assert airport.google_place_id == "ChIJ-lax"
