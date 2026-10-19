import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import app
from app.models import Place
from app.services.google_places import (
    FIELD_MASK,
    GooglePlaceNotFound,
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
    get_google_places_client,
    resolve_cover_photo,
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


def _enrich(client, headers, google_place_id):
    """Stub + refresh-google: the two steps that replaced POST /places/from-google."""
    stub = client.post("/places", json={"google_place_id": google_place_id}, headers=headers)
    assert stub.status_code in (200, 201), stub.text
    refreshed = client.post(f"/places/{stub.json()['id']}/refresh-google", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    return refreshed.json()


def test_create_auto_enriches_a_known_place(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE

    created = client.post("/places", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun)
    assert created.status_code == 201, created.text
    place = created.json()
    # Enriched in the create request, not left as a bare stub.
    assert place["name"] == "Hotel Kanade Shinjuku"
    assert place["timezone"] == "Asia/Tokyo"
    assert (place["latitude"], place["longitude"]) == (35.6938, 139.7034)
    # The raw Google response is cached but not exposed on the API (see cover_photo).
    assert place["cover_photo"] is None  # HOTEL_PLACE carries no photos
    assert place["google_fetched_at"] is not None
    assert google_places.calls == [HOTEL_PLACE_ID]  # enriched exactly once, at creation


def test_create_leaves_a_stub_when_google_cant_answer(client, make_user, google_places):
    sun = make_user("sun")  # HOTEL_PLACE_ID deliberately not registered in the fake

    created = client.post("/places", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun)
    assert created.status_code == 201, created.text
    assert created.json()["name"] is None  # a bare stub, to be refreshed later

    # Once Google knows it, refresh-google enriches the existing stub.
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    refreshed = client.post(f"/places/{created.json()['id']}/refresh-google", headers=sun)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["name"] == "Hotel Kanade Shinjuku"


def test_refresh_updates_the_cache_but_not_our_own_fields(client, make_user, google_places, session):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    place = client.post(
        "/places", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
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
    # The cache isn't on the API, so check the stored blob directly.
    cached = session.get(Place, uuid.UUID(place["id"])).google_data
    assert cached["displayName"]["text"] == "Hotel Kanade (renamed)"


def test_create_place_is_a_get_or_create_stub(client, make_user):
    sun = make_user("sun")
    created = client.post("/places", json={"google_place_id": "ChIJ-stub"}, headers=sun)
    assert created.status_code == 201, created.text
    place = created.json()
    # Only the id is ours to generate; the rest is filled in later.
    assert place["google_place_id"] == "ChIJ-stub"
    assert place["name"] is None
    assert place["address"] is None
    assert place["area"] is None
    assert place["id"] and place["created_at"] and place["updated_at"]

    again = client.post("/places", json={"google_place_id": "ChIJ-stub"}, headers=sun)
    assert again.status_code == 200
    assert again.json()["id"] == place["id"]


def test_google_place_id_is_unique_and_relinking_clears_the_cache(
    client, make_user, google_places, session
):
    sun = make_user("sun")
    google_places.places[HOTEL_PLACE_ID] = HOTEL_PLACE
    linked = _enrich(client, sun, HOTEL_PLACE_ID)

    # Relinking a different place onto an already-taken google_place_id conflicts.
    other = client.post("/places", json={"google_place_id": "ChIJ-other"}, headers=sun).json()
    duplicate = client.patch(
        f"/places/{other['id']}", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    )
    assert duplicate.status_code == 409

    # The failed update must not have poisoned the session for later requests.
    relinked = client.patch(
        f"/places/{linked['id']}", json={"google_place_id": "ChIJ-fresh"}, headers=sun
    )
    assert relinked.status_code == 200
    assert relinked.json()["google_fetched_at"] is None
    assert session.get(Place, uuid.UUID(linked["id"])).google_data is None


def test_google_failures_map_to_http_errors(client, make_user, session):
    sun = make_user("sun")

    # A Google id nobody knows -> 404 when we try to pull its details.
    missing_stub = client.post(
        "/places", json={"google_place_id": "ChIJ-missing"}, headers=sun
    ).json()
    missing = client.post(f"/places/{missing_stub['id']}/refresh-google", headers=sun)
    assert missing.status_code == 404

    # A place with no Google link (e.g. inserted directly) can't be refreshed.
    manual = Place(name="Secret ramen spot")
    session.add(manual)
    session.commit()
    unlinked = client.post(f"/places/{manual.id}/refresh-google", headers=sun)
    assert unlinked.status_code == 409

    # Google not configured -> 503.
    linked_stub = client.post(
        "/places", json={"google_place_id": HOTEL_PLACE_ID}, headers=sun
    ).json()
    app.dependency_overrides[get_google_places_client] = lambda: GooglePlacesClient(api_key=None)
    unconfigured = client.post(f"/places/{linked_stub['id']}/refresh-google", headers=sun)
    assert unconfigured.status_code == 503


def test_search_by_name(client, make_user, session):
    sun = make_user("sun")
    for name in ("Osaka Castle", "Kuromon Market", "Hotel Hanare Namba"):
        session.add(Place(name=name))
    session.commit()

    found = client.get("/places", params={"q": "osaka"}, headers=sun)
    assert [place["name"] for place in found.json()] == ["Osaka Castle"]
    # % is matched literally, not as a wildcard.
    assert client.get("/places", params={"q": "%"}, headers=sun).json() == []


def test_refresh_links_an_area_and_dedupes_it(client, make_user, google_places):
    sun = make_user("sun")
    google_places.places["ChIJ-louvre"] = _paris_poi("ChIJ-louvre", "Louvre Museum")
    google_places.places["ChIJ-eiffel"] = _paris_poi("ChIJ-eiffel", "Eiffel Tower")

    louvre = _enrich(client, sun, "ChIJ-louvre")
    assert louvre["area"]["name"] == "Paris"
    assert louvre["area_id"] == louvre["area"]["id"]

    # A second Paris place reuses the one Paris area rather than making another.
    eiffel = _enrich(client, sun, "ChIJ-eiffel")
    assert eiffel["area_id"] == louvre["area_id"]


def test_a_city_place_gets_no_area_of_its_own(client, make_user, google_places):
    sun = make_user("sun")
    paris_city = _paris_poi("ChIJ-paris", "Paris")
    paris_city["primaryType"] = "locality"
    paris_city["types"] = ["locality", "political"]
    google_places.places["ChIJ-paris"] = paris_city

    city = _enrich(client, sun, "ChIJ-paris")
    assert city["area"] is None


def test_manual_area_link(client, make_user):
    sun = make_user("sun")
    paris = client.post("/areas", json={"name": "Paris"}, headers=sun).json()

    poi = client.post("/places", json={"google_place_id": "ChIJ-champs"}, headers=sun).json()
    linked = client.patch(
        f"/places/{poi['id']}",
        json={"name": "Champs-Élysées", "area_id": paris["id"]},
        headers=sun,
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["area"]["name"] == "Paris"

    unknown = client.patch(
        f"/places/{poi['id']}", json={"area_id": str(uuid.uuid4())}, headers=sun
    )
    assert unknown.status_code == 422


def test_refresh_places_refreshes_only_stale_ones(client, make_user, google_places, session):
    sun = make_user("sun")
    now = datetime.now(UTC)
    google_places.places["g-stale"] = {"id": "g-stale", "displayName": {"text": "Fresh Name"}}
    stale = Place(
        google_place_id="g-stale",
        google_data={"displayName": {"text": "Old"}},
        google_fetched_at=now - timedelta(days=40),
    )
    fresh = Place(
        google_place_id="g-fresh",
        name="Kept",
        google_data={"photos": []},
        google_fetched_at=now,
    )
    session.add_all([stale, fresh])
    session.commit()
    stale_id, fresh_id = str(stale.id), str(fresh.id)

    resp = client.post("/places/refresh", json={"place_ids": [stale_id, fresh_id]}, headers=sun)
    assert resp.status_code == 200, resp.text
    places = resp.json()["places"]
    assert set(places) == {stale_id, fresh_id}
    assert google_places.calls == ["g-stale"]  # only the stale one hit Google
    assert places[stale_id]["name"] == "Fresh Name"  # refreshed
    assert places[fresh_id]["name"] == "Kept"  # left alone


def test_refresh_places_is_best_effort(client, make_user, google_places, session):
    sun = make_user("sun")  # g-unknown deliberately absent from the fake
    orphan = Place(
        google_place_id="g-unknown",
        name="Cached name",
        google_data={"displayName": {"text": "x"}},
        google_fetched_at=datetime.now(UTC) - timedelta(days=40),
    )
    session.add(orphan)
    session.commit()
    orphan_id = str(orphan.id)

    resp = client.post("/places/refresh", json={"place_ids": [orphan_id]}, headers=sun)
    assert resp.status_code == 200, resp.text
    # Google 404 is swallowed; the cached data is returned unchanged.
    assert resp.json()["places"][orphan_id]["name"] == "Cached name"
    assert google_places.calls == ["g-unknown"]


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


def _place_with_photos() -> Place:
    place = Place()
    place.google_fetched_at = datetime.now(UTC)
    place.google_data = {
        "photos": [
            {
                "name": "places/XYZ/photos/aaa",
                "authorAttributions": [
                    {"displayName": "Jane D", "uri": "https://maps.google.com/jane"}
                ],
            },
            {"name": "places/XYZ/photos/bbb", "authorAttributions": []},
        ]
    }
    return place


def test_resolve_cover_photo_builds_a_media_url_with_attribution():
    photo = resolve_cover_photo(_place_with_photos(), api_key="KEY", max_width_px=400)
    assert photo is not None
    # Defaults to the first photo, emits a loadable media URL and keeps the credit.
    assert photo.name == "places/XYZ/photos/aaa"
    assert photo.url == (
        "https://places.googleapis.com/v1/places/XYZ/photos/aaa/media?maxWidthPx=400&key=KEY"
    )
    assert [(a.display_name, a.uri) for a in photo.attributions] == [
        ("Jane D", "https://maps.google.com/jane")
    ]
    assert photo.is_stale is False


def test_resolve_cover_photo_prefers_a_selection_and_falls_back_when_it_vanishes():
    place = _place_with_photos()
    assert resolve_cover_photo(place, prefer_name="places/XYZ/photos/bbb", api_key="KEY").name == (
        "places/XYZ/photos/bbb"
    )
    # A selection that no longer exists after a refresh falls back to the first photo.
    assert resolve_cover_photo(place, prefer_name="places/XYZ/photos/gone", api_key="KEY").name == (
        "places/XYZ/photos/aaa"
    )


def test_resolve_cover_photo_flags_a_stale_cache():
    place = _place_with_photos()
    place.google_fetched_at = datetime.now(UTC) - timedelta(days=45)
    assert resolve_cover_photo(place, api_key="KEY").is_stale is True


def test_resolve_cover_photo_without_photos_needs_no_key():
    place = Place()
    place.google_data = {"id": "x"}  # enriched, but no photos
    assert resolve_cover_photo(place, api_key=None) is None


def test_resolve_cover_photo_raises_when_unconfigured_but_a_photo_exists(monkeypatch):
    # Force "no key configured" regardless of the developer's .env.
    from app.config import Settings
    from app.services import google_places as gp

    monkeypatch.setattr(gp, "get_settings", lambda: Settings(google_maps_api_key=None))
    with pytest.raises(GooglePlacesNotConfigured):
        resolve_cover_photo(_place_with_photos(), api_key=None)


def test_places_client_error_mapping():
    with pytest.raises(GooglePlacesNotConfigured):
        GooglePlacesClient(api_key=None).fetch_place(HOTEL_PLACE_ID)
    with pytest.raises(GooglePlaceNotFound):
        _client(lambda request: httpx.Response(404)).fetch_place("nope")
    with pytest.raises(GooglePlacesError):
        _client(lambda request: httpx.Response(500)).fetch_place(HOTEL_PLACE_ID)
