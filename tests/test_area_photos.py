import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Area, AreaPhoto
from app.services.unsplash import (
    UnsplashClient,
    UnsplashError,
    UnsplashNotConfigured,
    UnsplashPhoto,
    apply_area_photos,
)

SEARCH_RESULTS = {
    "results": [
        {
            "id": "photo-1",
            "width": 4000,
            "height": 3000,
            "color": "#262626",
            "urls": {"regular": "https://images.unsplash.com/photo-1?w=1080"},
            "links": {"download_location": "https://api.unsplash.com/photos/photo-1/download"},
            "user": {"name": "Jane Doe", "links": {"html": "https://unsplash.com/@jane"}},
        },
        {
            "id": "photo-2",
            "urls": {"regular": "https://images.unsplash.com/photo-2?w=1080"},
            "links": {"download_location": "https://api.unsplash.com/photos/photo-2/download"},
            "user": {"name": "John Roe", "links": {"html": "https://unsplash.com/@john"}},
        },
    ]
}


def _client(handler) -> UnsplashClient:
    transport = httpx.MockTransport(handler)
    return UnsplashClient("test-key", http=httpx.Client(transport=transport), app_name="outbound")


# --- the real client, against a stubbed transport ---


def test_search_sends_client_id_and_parses_photos():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=SEARCH_RESULTS)

    photos = _client(handler).search("Paris France", count=2)

    assert seen["auth"] == "Client-ID test-key"
    assert seen["query"]["query"] == "Paris France"
    assert seen["query"]["per_page"] == "2"
    assert [p.id for p in photos] == ["photo-1", "photo-2"]
    first = photos[0]
    assert first.url == "https://images.unsplash.com/photo-1?w=1080"
    assert first.attribution == "Jane Doe"
    # Profile link is UTM-tagged per Unsplash's guidelines.
    assert first.attribution_url == (
        "https://unsplash.com/@jane?utm_source=outbound&utm_medium=referral"
    )
    assert first.width == 4000 and first.color == "#262626"


def test_search_caps_results_at_count():
    photos = _client(lambda r: httpx.Response(200, json=SEARCH_RESULTS)).search("x", count=1)
    assert [p.id for p in photos] == ["photo-1"]


def test_search_without_key_raises_not_configured():
    client = UnsplashClient(None)
    with pytest.raises(UnsplashNotConfigured):
        client.search("Paris")


def test_search_maps_non_200_to_error():
    with pytest.raises(UnsplashError):
        _client(lambda r: httpx.Response(500)).search("Paris")


def test_trigger_download_pings_the_endpoint():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"url": "https://images.unsplash.com/photo-1"})

    _client(handler).trigger_download("https://api.unsplash.com/photos/photo-1/download")
    assert calls == ["https://api.unsplash.com/photos/photo-1/download"]


# --- apply_area_photos against the DB ---


def _photo(id_: str, url: str) -> UnsplashPhoto:
    return UnsplashPhoto(
        id=id_,
        url=url,
        attribution="Jane Doe",
        attribution_url="https://unsplash.com/@jane?utm_source=outbound&utm_medium=referral",
        download_location=None,
        width=4000,
        height=3000,
        color="#262626",
    )


def test_apply_area_photos_stores_ordered_rows_and_sets_default(session):
    area = Area(name="Paris", kind="city")
    session.add(area)
    session.flush()

    apply_area_photos(session, area, [_photo("a", "https://u/a"), _photo("b", "https://u/b")])
    session.commit()

    assert [(p.position, p.url, p.source) for p in area.photos] == [
        (0, "https://u/a", "unsplash"),
        (1, "https://u/b", "unsplash"),
    ]
    # First photo becomes the cover.
    assert area.default_photo_url == "https://u/a"


def test_apply_area_photos_keeps_a_manual_default(session):
    area = Area(name="Paris", kind="city", default_photo_url="https://manual/cover.jpg")
    session.add(area)
    session.flush()

    apply_area_photos(session, area, [_photo("a", "https://u/a")])
    session.commit()

    assert area.default_photo_url == "https://manual/cover.jpg"


def test_apply_area_photos_replaces_on_rerun(session):
    area = Area(name="Paris", kind="city")
    session.add(area)
    session.flush()

    apply_area_photos(session, area, [_photo("a", "https://u/a"), _photo("b", "https://u/b")])
    session.commit()
    apply_area_photos(session, area, [_photo("c", "https://u/c")])
    session.commit()

    assert [p.url for p in area.photos] == ["https://u/c"]


def test_same_photo_twice_violates_unique_constraint(session):
    area = Area(name="Paris", kind="city")
    session.add(area)
    session.flush()

    session.add(AreaPhoto(area_id=area.id, url="https://u/a", source="unsplash", source_id="a"))
    session.add(AreaPhoto(area_id=area.id, url="https://u/a2", source="unsplash", source_id="a"))
    with pytest.raises(IntegrityError):
        session.flush()


# --- exposure through the areas API ---


def test_area_read_exposes_photos(client, make_user, session):
    sun = make_user("sun")
    area = client.post("/areas", json={"name": "Paris", "kind": "city"}, headers=sun).json()

    session.add(
        AreaPhoto(
            area_id=area["id"],
            url="https://u/a",
            source="unsplash",
            source_id="a",
            attribution="Jane Doe",
            attribution_url="https://unsplash.com/@jane",
            position=0,
        )
    )
    session.commit()

    fetched = client.get(f"/areas/{area['id']}", headers=sun).json()
    assert len(fetched["photos"]) == 1
    photo = fetched["photos"][0]
    assert photo["url"] == "https://u/a"
    assert photo["source"] == "unsplash"
    assert photo["attribution"] == "Jane Doe"
    assert photo["attribution_url"] == "https://unsplash.com/@jane"
