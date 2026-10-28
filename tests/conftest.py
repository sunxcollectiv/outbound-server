import uuid
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
from app.models import User
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesClient,
    get_google_places_client,
)
from app.services.image_extraction import ImageExtractor, get_image_extractor
from app.services.mail_extraction import (
    ExtractedItem,
    Extraction,
    MailExtractor,
    get_mail_extractor,
)
from app.services.resend import ResendClient, ResendError, get_resend_client
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


class FakeResend(ResendClient):
    """Serves whatever the test loads into `received` (a list of email dicts,
    newest first) for both list and single-email fetch. Defaults to an empty
    inbox. Set `error` to make the list call raise, standing in for an upstream
    failure. `fetched` records which ids had their body retrieved."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.error: ResendError | None = None
        self.fetched: list[str] = []

    def list_received_emails(self, *, limit=None, before=None, after=None) -> dict[str, Any]:
        if self.error:
            raise self.error
        data = self.received if limit is None else self.received[:limit]
        return {"object": "list", "has_more": False, "data": data}

    def get_received_email(self, email_id: str, *, html_format=None) -> dict[str, Any]:
        self.fetched.append(email_id)
        for email in self.received:
            if email.get("id") == email_id:
                return email
        raise ResendError(f"Resend returned 404: {email_id} not found")


@pytest.fixture
def resend() -> FakeResend:
    return FakeResend()


class FakeMailExtractor(MailExtractor):
    """Returns a canned Extraction (a confident flight by default) and records
    its calls. Set `result` to change what it returns or `error` to make it
    raise, so tests drive the processed/rejected/failure branches without the
    real Claude call."""

    def __init__(self) -> None:
        self.result = Extraction(
            category="flight",
            confidence=0.95,
            summary="Flight to Tokyo",
            items=[ExtractedItem(kind="transit", transit_mode="flight", title="Flight to Tokyo")],
        )
        self.error: Exception | None = None
        self.calls: list[tuple[str | None, str | None]] = []

    def extract(self, *, subject=None, text_body=None) -> Extraction:
        self.calls.append((subject, text_body))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def mail_extractor() -> FakeMailExtractor:
    return FakeMailExtractor()


class FakeImageExtractor(ImageExtractor):
    """Returns a canned Extraction (a confident flight by default) and records its
    calls. Set `result` to change what it returns or `error` to make it raise, so
    tests drive the extracted/rejected/failure branches without the real Claude
    vision call."""

    def __init__(self) -> None:
        self.result = Extraction(
            category="flight",
            confidence=0.95,
            summary="Flight to Tokyo",
            items=[ExtractedItem(kind="transit", transit_mode="flight", title="Flight to Tokyo")],
        )
        self.error: Exception | None = None
        self.calls: list[tuple[bytes, str]] = []

    def extract(self, *, data: bytes, content_type: str) -> Extraction:
        self.calls.append((data, content_type))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def image_extractor() -> FakeImageExtractor:
    return FakeImageExtractor()


@pytest.fixture
def client(
    session: Session,
    google_places: FakeGooglePlaces,
    unsplash: FakeUnsplash,
    resend: FakeResend,
    mail_extractor: FakeMailExtractor,
    image_extractor: FakeImageExtractor,
) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_google_places_client] = lambda: google_places
    app.dependency_overrides[get_unsplash_client] = lambda: unsplash
    app.dependency_overrides[get_resend_client] = lambda: resend
    app.dependency_overrides[get_mail_extractor] = lambda: mail_extractor
    app.dependency_overrides[get_image_extractor] = lambda: image_extractor
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


PASSWORD = "correct horse battery"


class AuthHeaders(dict[str, str]):
    """Headers for acting as a user, plus their id and email for use in request bodies."""

    user_id: str
    email: str


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
        headers.email = body["user"]["email"]
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


@pytest.fixture
def make_admin(session: Session) -> Callable[[AuthHeaders], AuthHeaders]:
    """Makes an existing user an admin, the way scripts/make_admin.py does."""

    def _make(headers: AuthHeaders) -> AuthHeaders:
        user = session.get(User, uuid.UUID(headers.user_id))
        assert user is not None
        user.is_admin = True
        session.flush()
        return headers

    return _make


@pytest.fixture
def add_member(client: TestClient) -> Callable[..., dict[str, Any]]:
    """Adds `guest` to a trip by email as `organizer`. Returns the membership."""

    def _add(
        trip_id: str, organizer: dict[str, str], guest: AuthHeaders, role: str = "member"
    ) -> dict[str, Any]:
        payload = {"email": guest.email, "role": role}
        added = client.post(f"/trips/{trip_id}/members", json=payload, headers=organizer)
        assert added.status_code == 201, added.text
        return added.json()

    return _add


@pytest.fixture
def set_role(client: TestClient) -> Callable[..., dict[str, Any]]:
    """Changes one person's role by PATCHing the trip's whole roster, the way a
    client edits members. Returns the updated trip."""

    def _set(trip_id: str, organizer: dict[str, str], email: str, role: str) -> dict[str, Any]:
        trip = client.get(f"/trips/{trip_id}", headers=organizer).json()
        roster = [
            {"email": m["email"], "role": role if m["email"] == email else m["role"]}
            for m in trip["members"]
        ]
        response = client.patch(f"/trips/{trip_id}", json={"members": roster}, headers=organizer)
        assert response.status_code == 200, response.text
        return response.json()

    return _set
