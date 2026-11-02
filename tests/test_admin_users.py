def test_list_users_requires_admin(client, make_user):
    assert client.get("/admin/users").status_code == 401  # no token
    sun = make_user("sun")  # a normal, non-admin account
    assert client.get("/admin/users", headers=sun).status_code == 403


def test_list_users_returns_everyone(client, make_user, admin):
    make_user("alice")
    make_user("traveller")

    body = client.get("/admin/users", headers=admin).json()
    # The service token isn't a user, so only the two accounts show up.
    assert body["total"] == 2
    assert {u["email"] for u in body["items"]} == {"alice@example.com", "traveller@example.com"}
    row = body["items"][0]
    assert set(row) == {"id", "email", "display_name", "avatar_url", "created_at"}


def test_list_users_paginates(client, make_user, admin):
    for i in range(4):
        make_user(f"guest{i}")

    everyone = client.get("/admin/users?limit=100", headers=admin).json()
    assert everyone["total"] == 4
    ordered_ids = [u["id"] for u in everyone["items"]]

    page1 = client.get("/admin/users?limit=2&offset=0", headers=admin).json()
    page2 = client.get("/admin/users?limit=2&offset=2", headers=admin).json()
    assert page1["total"] == page2["total"] == 4
    assert [u["id"] for u in page1["items"]] == ordered_ids[:2]
    assert [u["id"] for u in page2["items"]] == ordered_ids[2:]
    assert not {u["id"] for u in page1["items"]} & {u["id"] for u in page2["items"]}


def test_list_users_filters_by_q(client, make_user, admin):
    make_user("alice")
    make_user("bob")

    by_email = client.get("/admin/users?q=alice", headers=admin).json()
    assert by_email["total"] == 1
    assert by_email["items"][0]["email"] == "alice@example.com"

    # ...and on display name, case-insensitively (make_user titles the name).
    by_name = client.get("/admin/users?q=BOB", headers=admin).json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["display_name"] == "Bob"

    assert client.get("/admin/users?q=nobody", headers=admin).json()["total"] == 0
