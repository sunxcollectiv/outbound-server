from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Area, Place

PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
# Places API (New) bills by field, so ask only for what a Place stores.
# addressComponents lets us pull out the containing city (see apply_area_from_google).
FIELD_MASK = (
    "id,displayName,formattedAddress,location,timeZone,primaryType,types,"
    "googleMapsUri,addressComponents"
)

# addressComponent types that name the city, best first. postalTown covers UK-style
# places with no locality; adminArea2 is a last resort (county/prefecture).
LOCALITY_TYPES = ("locality", "postal_town", "administrative_area_level_2")


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


def apply_google_place(place: Place, details: dict[str, Any]) -> None:
    """Refresh the Google cache on a place and fill in anything we don't have.

    Google's values never overwrite columns that already hold our own data.
    """
    place.google_data = details
    place.google_fetched_at = datetime.now(UTC)

    coords = details.get("location") or {}
    fallbacks = {
        "name": (details.get("displayName") or {}).get("text"),
        "address": details.get("formattedAddress"),
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "timezone": (details.get("timeZone") or {}).get("id"),
        "category": details.get("primaryType"),
    }
    for field, value in fallbacks.items():
        if getattr(place, field, None) is None and value is not None:
            setattr(place, field, value)


def _component(details: dict[str, Any], *types: str) -> str | None:
    """First address component whose types include any of `types`, by `types` order."""
    components = details.get("addressComponents") or []
    for wanted in types:
        for component in components:
            if wanted in (component.get("types") or []):
                return component.get("longText")
    return None


def apply_area_from_google(session: Session, place: Place, details: dict[str, Any]) -> None:
    """Point `place` at the area it sits in, get-or-creating that
    area. A no-op when the place already has a area or is itself a
    city. Parses the already-fetched Place Details response — no extra API call.
    """
    if place.area_id is not None:
        return
    place_types = set(details.get("types") or [])
    if details.get("primaryType"):
        place_types.add(details["primaryType"])
    if place_types.intersection(LOCALITY_TYPES):
        return  # the place is itself a city; it has no parent city to point at

    city = _component(details, *LOCALITY_TYPES)
    if not city or (place.name and city.casefold() == place.name.casefold()):
        return

    admin = _component(details, "administrative_area_level_1")
    country = _component(details, "country")
    region = ", ".join(part for part in (admin, country) if part) or None

    area = session.scalar(
        select(Area).where(
            func.lower(Area.name) == city.lower(),
            Area.address.is_not_distinct_from(region),
        )
    )
    if area is None:
        area = Area(name=city, address=region, created_by_id=place.created_by_id)
        session.add(area)
    place.area = area
