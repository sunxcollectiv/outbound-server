def test_list_users_requires_admin(client, make_user):
    assert client.get("/admin/users").status_code == 401  # no token
    sun = make_user("sun")  # a normal, non-admin account
    assert client.get("/admin/users", headers=sun).status_code == 403


def test_list_users_returns_everyone_with_admin_flag(client, make_user, make_admin):
    boss = make_admin(make_user("boss"))
    make_user("traveller")

    body = client.get("/admin/users", headers=boss).json()
    assert body["total"] == 2
    assert {u["email"] for u in body["items"]} == {"boss@example.com", "traveller@example.com"}
    flags = {u["email"]: u["is_admin"] for u in body["items"]}
    assert flags == {"boss@example.com": True, "traveller@example.com": False}
    # Shape of a row.
    row = body["items"][0]
    assert set(row) == {"id", "email", "display_name", "avatar_url", "created_at", "is_admin"}


def test_list_users_paginates(client, make_user, make_admin):
    boss = make_admin(make_user("boss"))
    for i in range(3):
        make_user(f"guest{i}")
    # boss + 3 guests = 4

    everyone = client.get("/admin/users?limit=100", headers=boss).json()
    assert everyone["total"] == 4
    ordered_ids = [u["id"] for u in everyone["items"]]

    page1 = client.get("/admin/users?limit=2&offset=0", headers=boss).json()
    page2 = client.get("/admin/users?limit=2&offset=2", headers=boss).json()
    assert page1["total"] == page2["total"] == 4
    assert [u["id"] for u in page1["items"]] == ordered_ids[:2]
    assert [u["id"] for u in page2["items"]] == ordered_ids[2:]
    # The two pages partition the whole set with no overlap.
    assert not {u["id"] for u in page1["items"]} & {u["id"] for u in page2["items"]}


def test_list_users_filters_by_q(client, make_user, make_admin):
    boss = make_admin(make_user("boss"))
    make_user("alice")
    make_user("bob")

    # Matches on email substring...
    by_email = client.get("/admin/users?q=alice", headers=boss).json()
    assert by_email["total"] == 1
    assert by_email["items"][0]["email"] == "alice@example.com"

    # ...and on display name, case-insensitively (make_user titles the name).
    by_name = client.get("/admin/users?q=BOB", headers=boss).json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["display_name"] == "Bob"

    assert client.get("/admin/users?q=nobody", headers=boss).json()["total"] == 0
