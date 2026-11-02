from datetime import UTC, datetime, timedelta


def test_service_token_can_use_admin_apis(client, make_service_token):
    svc = make_service_token("wravel-manage console")
    assert client.get("/admin/users", headers=svc).status_code == 200
    assert client.get("/admin/trips", headers=svc).status_code == 200


def test_service_token_resolves_to_no_user(client, make_service_token):
    """It authorizes the admin APIs but isn't a user, so user endpoints reject it."""
    svc = make_service_token("console")
    assert client.get("/users/me", headers=svc).status_code == 401


def test_non_admin_service_token_is_forbidden(client, make_service_token):
    svc = make_service_token("readonly", is_admin=False)
    assert client.get("/admin/users", headers=svc).status_code == 403


def test_expired_service_token_is_rejected(client, make_service_token):
    svc = make_service_token("old", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert client.get("/admin/users", headers=svc).status_code == 401


def test_never_expiring_service_token_is_accepted(client, make_service_token):
    svc = make_service_token("forever", expires_at=None)
    assert client.get("/admin/users", headers=svc).status_code == 200


def test_unknown_and_missing_tokens_are_rejected(client):
    assert client.get("/admin/users").status_code == 401
    assert client.get("/admin/users", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_admin_user_token_still_works(client, make_user, make_admin):
    boss = make_admin(make_user("boss"))
    assert client.get("/admin/users", headers=boss).status_code == 200


def test_service_token_posts_announcement_with_null_author(client, make_service_token, session):
    from app.models import Announcement

    svc = make_service_token("console")
    created = client.post(
        "/admin/announcements",
        json={"category": "announcement", "title": "Maintenance tonight"},
        headers=svc,
    )
    assert created.status_code == 201, created.text
    # No user behind a service token, so the row is attributed to nobody.
    row = session.get(Announcement, __import__("uuid").UUID(created.json()["id"]))
    assert row.created_by_id is None
