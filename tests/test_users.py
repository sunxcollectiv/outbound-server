def test_update_me_and_find_by_email(client, make_user):
    sun = make_user("sun")

    updated = client.patch("/users/me", json={"display_name": "Sun C."}, headers=sun)
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Sun C."

    found = client.get("/users", params={"email": "SUN@example.com"}, headers=sun)
    assert [user["id"] for user in found.json()] == [sun.user_id]


def test_users_cannot_be_created_outside_registration(client, make_user):
    sun = make_user("sun")
    payload = {"email": "other@example.com", "display_name": "Other"}
    assert client.post("/users", json=payload, headers=sun).status_code == 405
