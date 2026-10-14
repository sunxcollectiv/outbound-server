import uuid

import httpx
import pytest

from app.main import app
from app.services.google_places import (
    FIELD_MASK,
    GooglePlaceNotFound,
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
    get_google_places_client,
)

HOTEL_PLACE_ID = "ChIJ-test-hotel"
HOTEL_PLACE = {
    "id": HOTEL_PLACE_ID,
    "displayName": {"text": "Hotel Kanade Shinjuku", "languageCode": "en"},
    "formattedAddress": "1 Chome Kabukicho, Shinjuku City, Tokyo",
    "location": {"latitude": 35.6938, "longitude": 139.7034},
    "timeZone": {"id": "Asia/Tokyo"},
    "primaryType": "hotel",
    "googleMapsUri": "https://maps.google.com/?cid=1",
}


def _paris_poi(place_id: str, name: str) -> dict:
    """A Paris point of interest with the address components we parse a city from."""
    return {
        "id": place_id,
        "displayName": {"text": name},
        "formattedAddress": f"{name}, 75001 Paris, France",
        "primaryType": "tourist_attraction",
        "types": ["tourist_attraction", "point_of_interest"],
        "addressComponents": [
            {"longText": name, "shortText": name, "types": ["point_of_interest"]},
            {"longText": "Paris", "shortText": "Paris", "types": ["locality", "political"]},
            {
                "longText": "Île-de-France",
                "shortText": "IDF",
                "types": ["administrative_area_level_1", "political"],
            },
            {"longText": "France", "shortText": "FR", "types": ["country", "political"]},
        ],
    }


def test_from_google_is_get_or_create(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE

    created = client.post(
        "/places/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert created.status_code == 201, created.text
    place = created.json()
    assert place["name"] == "Hotel Kanade Shinjuku"
    assert place["timezone"] == "Asia/Tokyo"
    assert (place["latitude"], place["longitude"]) == (35.6938, 139.7034)
    assert place["google_data"] == HOTEL_PLACE
    assert place["google_fetched_at"] is not None

    again = client.post(
        "/places/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert again.status_code == 200
    assert again.json()["id"] == place["id"]
    assert google_places.calls == [HOTEL_PLACE_ID]  # no second Google request


def test_refresh_updates_the_cache_but_not_our_own_fields(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    place = client.post(
        "/places/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    ).json()
    url = f"/places/{place['id']}"

    ours = {"name": "Our hotel", "notes": "Ask for a high floor"}
    assert client.patch(url, json=ours, headers=sun).status_code == 200

    google_places.places[HOTEL_PLACE_ID] = {
        **HOTEL_PLACE,
        "displayName": {"text": "Hotel Kanade (renamed)"},
    }
    refreshed = client.post(f"{url}/refresh-google", headers=sun)
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["name"] == "Our hotel"
    assert body["notes"] == "Ask for a high floor"
    assert body["google_data"]["displayName"]["text"] == "Hotel Kanade (renamed)"


def test_google_place_id_is_unique_and_relinking_clears_the_cache(
    client, make_user, google_places
):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    linked = client.post(
        "/places/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    ).json()

    duplicate = client.post(
        "/places", json={"name": "Copy", "google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert duplicate.status_code == 409

    # The failed insert must not have poisoned the session for later requests.
    relinked = client.patch(
        f"/places/{linked['id']}", json={"google_place_id": "ChIJ-other"}, headers=sun
    )
    assert relinked.status_code == 200
    assert relinked.json()["google_data"] is None
    assert relinked.json()["google_fetched_at"] is None


def test_google_failures_map_to_http_errors(client, make_user):
    sun = make_user("sun")

    missing = client.post(
        "/places/from-google", json={"google_place_id": "ChIJ-missing"}, headers=sun
    )
    assert missing.status_code == 404

    manual = client.post("/places", json={"name": "Secret ramen spot"}, headers=sun).json()
    unlinked = client.post(f"/places/{manual['id']}/refresh-google", headers=sun)
    assert unlinked.status_code == 409

    app.dependency_overrides[get_google_places_client] = lambda: GooglePlacesClient(api_key=None)
    unconfigured = client.post(
        "/places/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert unconfigured.status_code == 503


def test_search_by_name(client, make_user):
    sun = make_user("sun")
    for name in ("Osaka Castle", "Kuromon Market", "Hotel Hanare Namba"):
        client.post("/places", json={"name": name}, headers=sun)

    found = client.get("/places", params={"q": "osaka"}, headers=sun)
    assert [place["name"] for place in found.json()] == ["Osaka Castle"]
    # % is matched literally, not as a wildcard.
    assert client.get("/places", params={"q": "%"}, headers=sun).json() == []


def test_from_google_links_a_area_and_dedupes_it(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places["ChIJ-louvre"] = _paris_poi("ChIJ-louvre", "Louvre Museum")
    google_places.places["ChIJ-eiffel"] = _paris_poi("ChIJ-eiffel", "Eiffel Tower")

    louvre = client.post(
        "/places/from-google", json={"google_place_id": "ChIJ-louvre"}, headers=sun
    ).json()
    assert louvre["area"]["name"] == "Paris"
    assert louvre["area_id"] == louvre["area"]["id"]

    # A second Paris place reuses the one Paris area rather than making another.
    eiffel = client.post(
        "/places/from-google", json={"google_place_id": "ChIJ-eiffel"}, headers=sun
    ).json()
    assert eiffel["area_id"] == louvre["area_id"]


def test_a_city_place_gets_no_area_of_its_own(client, make_user, google_places):
    sun = make_user("sun")
    paris_city = _paris_poi("ChIJ-paris", "Paris")
    paris_city["primaryType"] = "locality"
    paris_city["types"] = ["locality", "political"]
    google_places.places["ChIJ-paris"] = paris_city

    city = client.post(
        "/places/from-google", json={"google_place_id": "ChIJ-paris"}, headers=sun
    ).json()
    assert city["area"] is None


def test_manual_area_link(client, make_user):
    sun = make_user("sun")
    paris = client.post("/areas", json={"name": "Paris"}, headers=sun).json()

    poi = client.post(
        "/places", json={"name": "Champs-Élysées", "area_id": paris["id"]}, headers=sun
    )
    assert poi.status_code == 201, poi.text
    assert poi.json()["area"]["name"] == "Paris"

    unknown = client.post(
        "/places", json={"name": "Nowhere", "area_id": str(uuid.uuid4())}, headers=sun
    )
    assert unknown.status_code == 422


# --- the real client, against a stubbed transport ---


def _client(handler) -> GooglePlacesClient:
    return GooglePlacesClient("test-key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_places_client_sends_key_and_field_mask():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["X-Goog-Api-Key"]
        seen["mask"] = request.headers["X-Goog-FieldMask"]
        return httpx.Response(200, json=HOTEL_PLACE)

    assert _client(handler).fetch_place(HOTEL_PLACE_ID) == HOTEL_PLACE
    assert seen == {
        "url": f"https://places.googleapis.com/v1/places/{HOTEL_PLACE_ID}",
        "key": "test-key",
        "mask": FIELD_MASK,
    }


def test_places_client_error_mapping():
    with pytest.raises(GooglePlacesNotConfigured):
        GooglePlacesClient(api_key=None).fetch_place(HOTEL_PLACE_ID)
    with pytest.raises(GooglePlaceNotFound):
        _client(lambda request: httpx.Response(404)).fetch_place("nope")
    with pytest.raises(GooglePlacesError):
        _client(lambda request: httpx.Response(500)).fetch_place(HOTEL_PLACE_ID)
