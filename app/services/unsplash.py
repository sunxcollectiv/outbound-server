from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Area, AreaPhoto

UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
DEFAULT_PHOTO_COUNT = 3
SOURCE = "unsplash"
# The image size we hotlink. Unsplash's `regular` is ~1080px wide — a good
# default for a cover image without pulling the multi-MB raw file.
PHOTO_SIZE = "regular"


class UnsplashError(Exception):
    pass


class UnsplashNotConfigured(UnsplashError):
    pass


@dataclass(frozen=True)
class UnsplashPhoto:
    """A photo from Unsplash search, trimmed to what AreaPhoto stores.

    `url` is a hotlink to Unsplash's CDN (safe to store and serve). `attribution`
    / `attribution_url` are the photographer credit Unsplash's guidelines require
    us to show. `download_location` must be pinged when the photo is actually
    used (see `UnsplashClient.trigger_download`).
    """

    id: str
    url: str
    attribution: str | None
    attribution_url: str | None
    download_location: str | None
    width: int | None
    height: int | None
    color: str | None


def _attribution_url(user: dict[str, Any], app_name: str) -> str | None:
    """A UTM-tagged link to the photographer's Unsplash profile, as the API
    guidelines require (so Unsplash can attribute referrals back to us)."""
    html = (user.get("links") or {}).get("html")
    if not html:
        return None
    parsed = urlparse(html)
    query = urlencode({"utm_source": app_name, "utm_medium": "referral"})
    return urlunparse(parsed._replace(query=query))


def _parse_photo(result: dict[str, Any], app_name: str) -> UnsplashPhoto | None:
    url = (result.get("urls") or {}).get(PHOTO_SIZE)
    if not url:
        return None
    user = result.get("user") or {}
    return UnsplashPhoto(
        id=result.get("id", ""),
        url=url,
        attribution=user.get("name"),
        attribution_url=_attribution_url(user, app_name),
        download_location=(result.get("links") or {}).get("download_location"),
        width=result.get("width"),
        height=result.get("height"),
        color=result.get("color"),
    )


class UnsplashClient:
    def __init__(
        self,
        access_key: str | None,
        http: httpx.Client | None = None,
        app_name: str = "outbound",
    ) -> None:
        self._access_key = access_key
        self._app_name = app_name
        self._http = http or httpx.Client(timeout=10)

    def _auth(self) -> dict[str, str]:
        if not self._access_key:
            raise UnsplashNotConfigured("UNSPLASH_ACCESS_KEY is not set")
        return {"Authorization": f"Client-ID {self._access_key}"}

    def search(self, query: str, *, count: int = DEFAULT_PHOTO_COUNT) -> list[UnsplashPhoto]:
        """The top `count` landscape photos matching `query`, best match first."""
        headers = self._auth()
        try:
            response = self._http.get(
                UNSPLASH_SEARCH_URL,
                headers=headers,
                params={"query": query, "per_page": count, "orientation": "landscape"},
            )
        except httpx.HTTPError as exc:
            raise UnsplashError(f"Unsplash request failed: {exc}") from exc
        if response.status_code != 200:
            raise UnsplashError(f"Unsplash returned {response.status_code}")

        results = response.json().get("results") or []
        photos = [_parse_photo(result, self._app_name) for result in results[:count]]
        return [photo for photo in photos if photo is not None]

    def trigger_download(self, download_location: str | None) -> None:
        """Ping a photo's download endpoint. Unsplash's guidelines require this
        whenever a photo is used/displayed. Best-effort — never block on it."""
        if not download_location or not self._access_key:
            return
        try:
            self._http.get(download_location, headers=self._auth())
        except httpx.HTTPError:
            pass


def get_unsplash_client() -> UnsplashClient:
    settings = get_settings()
    return UnsplashClient(settings.unsplash_access_key, app_name=settings.unsplash_app_name)


def search_query_for(area: Area) -> str:
    """A search query for an area. Cities get their country for context
    ("Kyoto Japan") so we don't match a same-named place elsewhere."""
    if area.kind == "city" and area.parent is not None:
        return f"{area.name} {area.parent.name}"
    return area.name


def ensure_area_photos(session: Session, client: "UnsplashClient", areas: list[Area]) -> None:
    """Best-effort: fill in photos for any of `areas` that has none yet, so the
    caller can hand back cover images.

    Every Unsplash failure is swallowed — a request never fails because of
    photos — and the first failure (not configured, rate-limited, network) stops
    the rest rather than hammering a source that won't recover within the call.
    """
    changed = False
    for area in (a for a in areas if not a.photos):
        try:
            photos = client.search(search_query_for(area), count=DEFAULT_PHOTO_COUNT)
        except UnsplashError:
            break
        if photos:
            apply_area_photos(session, area, photos)
            for photo in photos:
                client.trigger_download(photo.download_location)
            changed = True
    if changed:
        session.commit()


def apply_area_photos(session: Session, area: Area, photos: list[UnsplashPhoto]) -> None:
    """Replace `area`'s photos with `photos` (ordered), and set the area's
    `default_photo_url` to the first one when it isn't set by hand already
    (fill-only-empty, like the Google place enrichment)."""
    area.photos[:] = [
        AreaPhoto(
            url=photo.url,
            source=SOURCE,
            source_id=photo.id or None,
            attribution=photo.attribution,
            attribution_url=photo.attribution_url,
            position=position,
            width=photo.width,
            height=photo.height,
            color=photo.color,
        )
        for position, photo in enumerate(photos)
    ]
    if photos and not area.default_photo_url:
        area.default_photo_url = photos[0].url
    session.add(area)
