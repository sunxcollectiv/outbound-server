def test_list_trips_requires_admin(client, make_user):
    assert client.get("/admin/trips").status_code == 401  # no token
    sun = make_user("sun")  # a normal, non-admin account
    assert client.get("/admin/trips", headers=sun).status_code == 403


def test_list_trips_returns_all_with_owner_and_member_count(client, make_user, admin, make_trip):
    boss = make_user("boss")
    traveller = make_user("traveller")
    make_trip(boss, title="Patagonia")
    make_trip(traveller, title="Tokyo")

    body = client.get("/admin/trips", headers=admin).json()
    assert body["total"] == 2
    by_title = {t["title"]: t for t in body["items"]}
    assert set(by_title) == {"Patagonia", "Tokyo"}
    assert by_title["Patagonia"]["owner_email"] == "boss@example.com"
    assert by_title["Tokyo"]["owner_email"] == "traveller@example.com"
    # The owner is the one member on a fresh trip.
    assert by_title["Patagonia"]["member_count"] == 1
    row = body["items"][0]
    assert set(row) == {
        "id",
        "title",
        "owner_email",
        "start_date",
        "end_date",
        "member_count",
        "created_at",
    }


def test_list_trips_paginates(client, make_user, admin, make_trip):
    owner = make_user("boss")
    for i in range(3):
        make_trip(owner, title=f"Trip {i}")

    everyone = client.get("/admin/trips?limit=100", headers=admin).json()
    assert everyone["total"] == 3
    ordered_ids = [t["id"] for t in everyone["items"]]

    page1 = client.get("/admin/trips?limit=2&offset=0", headers=admin).json()
    page2 = client.get("/admin/trips?limit=2&offset=2", headers=admin).json()
    assert page1["total"] == page2["total"] == 3
    assert [t["id"] for t in page1["items"]] == ordered_ids[:2]
    assert [t["id"] for t in page2["items"]] == ordered_ids[2:]


def test_list_trips_filters_by_q(client, make_user, admin, make_trip):
    owner = make_user("boss")
    make_trip(owner, title="Paris in spring")
    make_trip(owner, title="Tokyo autumn")

    body = client.get("/admin/trips?q=paris", headers=admin).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Paris in spring"
    assert client.get("/admin/trips?q=nothing", headers=admin).json()["total"] == 0
