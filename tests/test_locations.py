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


def test_from_google_is_get_or_create(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE

    created = client.post(
        "/locations/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert created.status_code == 201, created.text
    location = created.json()
    assert location["name"] == "Hotel Kanade Shinjuku"
    assert location["timezone"] == "Asia/Tokyo"
    assert (location["latitude"], location["longitude"]) == (35.6938, 139.7034)
    assert location["google_data"] == HOTEL_PLACE
    assert location["google_fetched_at"] is not None

    again = client.post(
        "/locations/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert again.status_code == 200
    assert again.json()["id"] == location["id"]
    assert google_places.calls == [HOTEL_PLACE_ID]  # no second Google request


def test_refresh_updates_the_cache_but_not_our_own_fields(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    location = client.post(
        "/locations/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    ).json()
    url = f"/locations/{location['id']}"

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
        "/locations/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    ).json()

    duplicate = client.post(
        "/locations", json={"name": "Copy", "google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert duplicate.status_code == 409

    # The failed insert must not have poisoned the session for later requests.
    relinked = client.patch(
        f"/locations/{linked['id']}", json={"google_place_id": "ChIJ-other"}, headers=sun
    )
    assert relinked.status_code == 200
    assert relinked.json()["google_data"] is None
    assert relinked.json()["google_fetched_at"] is None


def test_google_failures_map_to_http_errors(client, make_user):
    sun = make_user("sun")

    missing = client.post(
        "/locations/from-google", json={"google_place_id": "ChIJ-missing"}, headers=sun
    )
    assert missing.status_code == 404

    manual = client.post("/locations", json={"name": "Secret ramen spot"}, headers=sun).json()
    unlinked = client.post(f"/locations/{manual['id']}/refresh-google", headers=sun)
    assert unlinked.status_code == 409

    app.dependency_overrides[get_google_places_client] = lambda: GooglePlacesClient(api_key=None)
    unconfigured = client.post(
        "/locations/from-google", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert unconfigured.status_code == 503


def test_search_by_name(client, make_user):
    sun = make_user("sun")
    for name in ("Osaka Castle", "Kuromon Market", "Hotel Hanare Namba"):
        client.post("/locations", json={"name": name}, headers=sun)

    found = client.get("/locations", params={"q": "osaka"}, headers=sun)
    assert [location["name"] for location in found.json()] == ["Osaka Castle"]
    # % is matched literally, not as a wildcard.
    assert client.get("/locations", params={"q": "%"}, headers=sun).json() == []


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
