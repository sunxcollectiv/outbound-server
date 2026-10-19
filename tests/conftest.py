from collections.abc import Callable, Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesClient,
    get_google_places_client,
)
from app.services.unsplash import UnsplashClient, UnsplashPhoto, get_unsplash_client

TEST_DATABASE = "outbound_test"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A scratch database on the dev server, built by running the real migrations."""
    url = make_url(get_settings().database_url)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    admin.dispose()

    test_url = url.set(database=TEST_DATABASE)
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option(
        "sqlalchemy.url", test_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(alembic_config, "head")

    engine = create_engine(test_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Each test runs inside a transaction that is rolled back afterwards;
    commits made by the app become savepoints."""
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(
            connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as session:
            yield session
        transaction.rollback()


class FakeGooglePlaces(GooglePlacesClient):
    def __init__(self) -> None:
        self.places: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def fetch_place(self, place_id: str) -> dict[str, Any]:
        self.calls.append(place_id)
        if place_id not in self.places:
            raise GooglePlaceNotFound(place_id)
        return self.places[place_id]


@pytest.fixture
def google_places() -> FakeGooglePlaces:
    return FakeGooglePlaces()


class FakeUnsplash(UnsplashClient):
    """Returns whatever photos the test puts in `results` for any query, and
    records the queries/downloads it was asked for. Defaults to no results, so
    on-demand area-photo fetching is a no-op unless a test opts in."""

    def __init__(self) -> None:
        self.results: list[UnsplashPhoto] = []
        self.queries: list[str] = []
        self.downloads: list[str | None] = []

    def search(self, query: str, *, count: int = 3) -> list[UnsplashPhoto]:
        self.queries.append(query)
        return self.results[:count]

    def trigger_download(self, download_location: str | None) -> None:
        self.downloads.append(download_location)


@pytest.fixture
def unsplash() -> FakeUnsplash:
    return FakeUnsplash()


@pytest.fixture
def client(
    session: Session, google_places: FakeGooglePlaces, unsplash: FakeUnsplash
) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_google_places_client] = lambda: google_places
    app.dependency_overrides[get_unsplash_client] = lambda: unsplash
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


PASSWORD = "correct horse battery"


class AuthHeaders(dict[str, str]):
    """Headers for acting as a user, plus their id for use in request bodies."""

    user_id: str


@pytest.fixture
def make_user(client: TestClient) -> Callable[[str], AuthHeaders]:
    """Registers a user and returns the auth headers for acting as them."""

    def _make(name: str) -> AuthHeaders:
        response = client.post(
            "/auth/register",
            json={
                "email": f"{name}@example.com",
                "password": PASSWORD,
                "display_name": name.title(),
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        headers = AuthHeaders(Authorization=f"Bearer {body['token']}")
        headers.user_id = body["user"]["id"]
        return headers

    return _make


JAPAN_TRIP = {
    "title": "Japan",
    "start_date": "2026-04-09",
    "end_date": "2026-04-15",
}


@pytest.fixture
def make_trip(client: TestClient) -> Callable[..., dict[str, Any]]:
    def _make(headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
        response = client.post("/trips", json={**JAPAN_TRIP, **overrides}, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _make
