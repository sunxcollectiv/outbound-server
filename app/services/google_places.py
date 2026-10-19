from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Area, Place

PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
# The Place Photo (New) media endpoint. {photo_name} is a photo resource name
# from Place Details, e.g. "places/<place_id>/photos/<ref>". The endpoint 302s
# to a short-lived googleusercontent.com CDN URL, so this URL is safe to hand to
# a client <img src> but the redirect target must never be stored.
PLACE_PHOTO_MEDIA_URL = "https://places.googleapis.com/v1/{photo_name}/media"
DEFAULT_PHOTO_MAX_WIDTH_PX = 800

# Places API (New) bills by field, so ask only for what a Place stores.
# addressComponents lets us pull out the containing city (see apply_area_from_google).
# photos carries each photo's resource name + authorAttributions, which we must
# display alongside the image.
FIELD_MASK = (
    "id,displayName,formattedAddress,location,timeZone,primaryType,types,"
    "googleMapsUri,addressComponents,photos"
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
    """Point `place` at the area it sits in, get-or-creating that area. A no-op
    when the place already has an area or is itself a city. Parses the
    already-fetched Place Details response — no extra API call.

    Areas are deduped by name alone for now; same-named cities would share one.
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

    area = session.scalar(select(Area).where(func.lower(Area.name) == city.lower()))
    if area is None:
        area = Area(name=city)
        session.add(area)
    place.area = area


@dataclass(frozen=True)
class PhotoAttribution:
    """Credit Google requires us to show next to a place photo."""

    display_name: str | None
    uri: str | None


@dataclass(frozen=True)
class ResolvedPhoto:
    url: str  # ready for an <img src>; redirects to Google's CDN at load time
    name: str  # the photo resource name this resolved to
    attributions: list[PhotoAttribution]
    is_stale: bool  # cache older than the 30-day cap; refresh before trusting


def build_photo_url(
    photo_name: str, *, api_key: str, max_width_px: int = DEFAULT_PHOTO_MAX_WIDTH_PX
) -> str:
    """Turn a photo resource name into a loadable Place Photo media URL.

    The API key rides in the URL, so restrict the key (HTTP-referrer or a
    server-side proxy) before exposing this to a browser.
    """
    query = urlencode({"maxWidthPx": max_width_px, "key": api_key})
    return f"{PLACE_PHOTO_MEDIA_URL.format(photo_name=photo_name)}?{query}"


def _pick_photo(photos: list[dict[str, Any]], prefer_name: str | None) -> dict[str, Any] | None:
    """The chosen cover if its reference still exists, else the first photo.

    Google doesn't guarantee a photo survives a refresh, so a vanished selection
    falls back rather than erroring.
    """
    if prefer_name:
        for photo in photos:
            if photo.get("name") == prefer_name:
                return photo
    return photos[0] if photos else None


def _cache_is_stale(fetched_at: datetime | None, now: datetime) -> bool:
    if fetched_at is None:
        return True
    max_age = timedelta(days=get_settings().google_photo_max_age_days)
    return now - fetched_at > max_age


def resolve_cover_photo(
    place: Place,
    *,
    prefer_name: str | None = None,
    max_width_px: int = DEFAULT_PHOTO_MAX_WIDTH_PX,
    api_key: str | None = None,
    now: datetime | None = None,
) -> ResolvedPhoto | None:
    """Build a ready-to-load cover photo URL from a place's cached Google data.

    Reads photo references out of `place.google_data` (no API call), picks the
    preferred photo or the first one, and emits a media URL plus the attributions
    to display. Returns None when the place has no cached photos. `prefer_name`
    is the caller's stored cover selection, if any; `is_stale` on the result
    signals the cache is past the 30-day cap and the place should be refreshed.
    """
    photos = (place.google_data or {}).get("photos") or []
    chosen = _pick_photo(photos, prefer_name)
    if chosen is None or not chosen.get("name"):
        return None

    # Only a place that actually has a photo needs the key, so the no-photo case
    # (by far the common one) resolves to None even when Google isn't configured.
    api_key = api_key or get_settings().google_maps_api_key
    if not api_key:
        raise GooglePlacesNotConfigured("GOOGLE_MAPS_API_KEY is not set")

    return ResolvedPhoto(
        url=build_photo_url(chosen["name"], api_key=api_key, max_width_px=max_width_px),
        name=chosen["name"],
        attributions=[
            PhotoAttribution(a.get("displayName"), a.get("uri"))
            for a in (chosen.get("authorAttributions") or [])
        ],
        is_stale=_cache_is_stale(place.google_fetched_at, now or datetime.now(UTC)),
    )
