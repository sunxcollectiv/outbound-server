from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.models import Location

PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
# Places API (New) bills by field, so ask only for what Location stores.
FIELD_MASK = "id,displayName,formattedAddress,location,timeZone,primaryType,types,googleMapsUri"


class GooglePlacesError(Exception):
    pass


class GooglePlacesNotConfigured(GooglePlacesError):
    pass


class GooglePlaceNotFound(GooglePlacesError):
    pass


class GooglePlacesClient:
    def __init__(self, api_key: str | None, http: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=10)

    def fetch_place(self, place_id: str) -> dict[str, Any]:
        if not self._api_key:
            raise GooglePlacesNotConfigured("GOOGLE_MAPS_API_KEY is not set")
        try:
            response = self._http.get(
                PLACE_DETAILS_URL.format(place_id=place_id),
                headers={"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": FIELD_MASK},
            )
        except httpx.HTTPError as exc:
            raise GooglePlacesError(f"Google Places request failed: {exc}") from exc
        # Google answers 400 for a malformed id and 404 for an unknown one.
        if response.status_code in (400, 404):
            raise GooglePlaceNotFound(place_id)
        if response.status_code != 200:
            raise GooglePlacesError(f"Google Places returned {response.status_code}")
        return response.json()


def get_google_places_client() -> GooglePlacesClient:
    return GooglePlacesClient(get_settings().google_maps_api_key)


def apply_google_place(location: Location, place: dict[str, Any]) -> None:
    """Refresh the Google cache on a location and fill in anything we don't have.

    Google's values never overwrite columns that already hold our own data.
    """
    location.google_data = place
    location.google_fetched_at = datetime.now(UTC)

    coords = place.get("location") or {}
    fallbacks = {
        "name": (place.get("displayName") or {}).get("text"),
        "address": place.get("formattedAddress"),
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "timezone": (place.get("timeZone") or {}).get("id"),
        "category": place.get("primaryType"),
    }
    for field, value in fallbacks.items():
        if getattr(location, field, None) is None and value is not None:
            setattr(location, field, value)
