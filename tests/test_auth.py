from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.models import AccessToken, User
from tests.conftest import PASSWORD

SUN = {"email": "Sun@Example.com", "password": PASSWORD, "display_name": "Sun"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_normalises_email_and_rejects_duplicates(client):
    first = client.post("/auth/register", json=SUN)
    assert first.status_code == 201
    assert first.json()["user"]["email"] == "sun@example.com"

    me = client.get("/users/me", headers=_bearer(first.json()["token"]))
    assert me.json()["id"] == first.json()["user"]["id"]

    duplicate = client.post("/auth/register", json={**SUN, "email": "sun@example.com"})
    assert duplicate.status_code == 409


def test_register_requires_a_reasonable_password(client):
    assert client.post("/auth/register", json={**SUN, "password": "short"}).status_code == 422
    without_password = {key: value for key, value in SUN.items() if key != "password"}
    assert client.post("/auth/register", json=without_password).status_code == 422


def test_passwords_and_tokens_are_not_stored_or_returned_in_the_clear(client, session):
    registered = client.post("/auth/register", json=SUN).json()
    assert "password" not in str(registered["user"])

    user = session.scalar(select(User))
    assert user.password_hash.startswith("$argon2")
    assert session.scalar(select(AccessToken.token_hash)) != registered["token"]


def test_login(client):
    client.post("/auth/register", json=SUN)

    # Email is matched case-insensitively, like registration.
    login = client.post("/auth/login", json={"email": "SUN@example.com", "password": PASSWORD})
    assert login.status_code == 200
    me = client.get("/users/me", headers=_bearer(login.json()["token"]))
    assert me.json()["email"] == "sun@example.com"

    wrong_password = client.post(
        "/auth/login", json={"email": "sun@example.com", "password": "nope"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_requests_without_a_valid_token_are_rejected(client, make_user):
    sun = make_user("sun")

    assert client.get("/users/me").status_code == 401
    assert client.get("/users/me", headers=_bearer("made-up")).status_code == 401
    # The old dev-only header no longer identifies anyone.
    assert client.get("/users/me", headers={"X-User-Id": sun.user_id}).status_code == 401


def test_expired_tokens_are_rejected(client, make_user, session):
    sun = make_user("sun")
    assert client.get("/users/me", headers=sun).status_code == 200

    session.execute(update(AccessToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert client.get("/users/me", headers=sun).status_code == 401


def test_logout_only_ends_that_login(client, make_user):
    phone = make_user("sun")
    laptop = _bearer(
        client.post(
            "/auth/login", json={"email": "sun@example.com", "password": PASSWORD}
        ).json()["token"]
    )

    assert client.post("/auth/logout", headers=phone).status_code == 204
    assert client.get("/users/me", headers=phone).status_code == 401
    assert client.get("/users/me", headers=laptop).status_code == 200
