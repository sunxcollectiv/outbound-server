import uuid


def test_area_crud(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))

    created = client.post(
        "/admin/areas",
        json={
            "name": "Paris",
            "kind": "city",
            "level": 3,
            "default_photo_url": "https://x/p.jpg",
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    area = created.json()
    assert area["name"] == "Paris"
    assert area["kind"] == "city"
    assert area["level"] == 3
    assert area["default_photo_url"] == "https://x/p.jpg"
    assert area["default_photo_attribution"] is None
    assert "photos" not in area  # candidate photos aren't on the ordinary read

    admin_url = f"/admin/areas/{area['id']}"
    read_url = f"/areas/{area['id']}"
    patched = client.patch(admin_url, json={"name": "Paris, FR", "level": 4}, headers=sun)
    assert patched.status_code == 200
    assert patched.json()["name"] == "Paris, FR"
    assert patched.json()["level"] == 4
    assert client.patch(admin_url, json={"name": None}, headers=sun).status_code == 422

    assert client.get(read_url, headers=sun).json()["name"] == "Paris, FR"
    assert client.delete(admin_url, headers=sun).status_code == 204
    assert client.get(read_url, headers=sun).status_code == 404


def test_area_writes_are_admin_only(client, make_user):
    """Create/update/delete live under /admin; a normal user is forbidden,
    but the client-facing search still works for anyone authenticated."""
    sun = make_user("sun")  # non-admin
    assert client.post("/admin/areas", json={"name": "Paris"}, headers=sun).status_code == 403
    some_id = uuid.uuid4()
    assert client.patch(f"/admin/areas/{some_id}", json={"name": "x"}, headers=sun).status_code == 403
    assert client.delete(f"/admin/areas/{some_id}", headers=sun).status_code == 403
    # Search stays open to any authenticated user.
    assert client.get("/areas", params={"q": "paris"}, headers=sun).status_code == 200


def test_area_search_by_name(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    for name in ("Paris", "Lyon", "Marseille"):
        client.post("/admin/areas", json={"name": name}, headers=sun)

    found = client.get("/areas", params={"q": "lyo"}, headers=sun)
    assert [area["name"] for area in found.json()] == ["Lyon"]


def test_area_kind_and_level_default_to_null(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    area = client.post("/admin/areas", json={"name": "Kreuzberg"}, headers=sun).json()
    assert area["kind"] is None
    assert area["level"] is None


def test_area_search_ranks_exact_then_prefix_then_shorter(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    for name, kind in [
        ("Paraguay", "country"),
        ("Paris", "city"),
        ("Pardubice", "city"),
        ("Cannes-la-Bocca Paris annex", "city"),  # substring, not prefix
    ]:
        client.post("/admin/areas", json={"name": name, "kind": kind}, headers=sun)

    names = [a["name"] for a in client.get("/areas", params={"q": "par"}, headers=sun).json()]
    # Prefix matches (shorter first) come before the substring-only match.
    assert names[0] == "Paris"  # shortest prefix match
    assert names.index("Paraguay") < names.index("Cannes-la-Bocca Paris annex")
    assert names[-1] == "Cannes-la-Bocca Paris annex"


def test_area_search_exact_match_wins(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    client.post("/admin/areas", json={"name": "Nice city nearby", "kind": "city"}, headers=sun)
    client.post("/admin/areas", json={"name": "Nice", "kind": "city"}, headers=sun)

    names = [a["name"] for a in client.get("/areas", params={"q": "nice"}, headers=sun).json()]
    assert names[0] == "Nice"


def test_area_search_filter_by_kind(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    client.post("/admin/areas", json={"name": "Georgia", "kind": "country"}, headers=sun)
    client.post("/admin/areas", json={"name": "Georgetown", "kind": "city"}, headers=sun)

    cities = client.get("/areas", params={"q": "georg", "kind": "city"}, headers=sun).json()
    assert [a["name"] for a in cities] == ["Georgetown"]


def test_area_search_includes_parent(client, make_user, make_admin):
    sun = make_admin(make_user("sun"))
    france = client.post(
        "/admin/areas", json={"name": "France", "kind": "country"}, headers=sun
    ).json()
    client.post(
        "/admin/areas",
        json={"name": "Paris", "kind": "city", "parent_id": france["id"]},
        headers=sun,
    )

    paris = client.get("/areas", params={"q": "paris"}, headers=sun).json()[0]
    assert paris["parent_id"] == france["id"]
    assert paris["parent"]["name"] == "France"
    assert paris["parent"]["kind"] == "country"


def test_unknown_area_404s(client, make_user):
    sun = make_user("sun")
    assert client.get(f"/areas/{uuid.uuid4()}", headers=sun).status_code == 404
