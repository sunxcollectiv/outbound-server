def test_create_user_normalises_email_and_rejects_duplicates(client):
    first = client.post("/users", json={"email": "Sun@Example.com", "display_name": "Sun"})
    assert first.status_code == 201
    assert first.json()["email"] == "sun@example.com"

    duplicate = client.post("/users", json={"email": "sun@example.com", "display_name": "Other"})
    assert duplicate.status_code == 409


def test_requests_without_a_known_user_are_rejected(client):
    assert client.get("/users/me").status_code == 401
    unknown = {"X-User-Id": "00000000-0000-0000-0000-000000000000"}
    assert client.get("/users/me", headers=unknown).status_code == 401


def test_update_me_and_find_by_email(client, make_user):
    sun = make_user("sun")

    updated = client.patch("/users/me", json={"display_name": "Sun C."}, headers=sun)
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Sun C."

    found = client.get("/users", params={"email": "SUN@example.com"}, headers=sun)
    assert [user["id"] for user in found.json()] == [sun["X-User-Id"]]
