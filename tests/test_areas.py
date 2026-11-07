import uuid


def test_area_crud(client, make_user, admin):
    sun = make_user("sun")  # reads are open to any authenticated user

    created = client.post(
        "/admin/areas",
        json={
            "name": "Paris",
            "kind": "city",
            "level": 3,
            "default_photo_url": "https://x/p.jpg",
        },
        headers=admin,
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
    patched = client.patch(admin_url, json={"name": "Paris, FR", "level": 4}, headers=admin)
    assert patched.status_code == 200
    assert patched.json()["name"] == "Paris, FR"
    assert patched.json()["level"] == 4
    assert client.patch(admin_url, json={"name": None}, headers=admin).status_code == 422

    assert client.get(read_url, headers=sun).json()["name"] == "Paris, FR"
    assert client.delete(admin_url, headers=admin).status_code == 204
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


def test_area_search_by_name(client, make_user, admin):
    sun = make_user("sun")
    for name in ("Paris", "Lyon", "Marseille"):
        client.post("/admin/areas", json={"name": name}, headers=admin)

    found = client.get("/areas", params={"q": "lyo"}, headers=sun)
    assert [area["name"] for area in found.json()] == ["Lyon"]


def test_area_kind_and_level_default_to_null(client, admin):
    area = client.post("/admin/areas", json={"name": "Kreuzberg"}, headers=admin).json()
    assert area["kind"] is None
    assert area["level"] is None


def test_area_search_ranks_exact_then_prefix_then_shorter(client, make_user, admin):
    sun = make_user("sun")
    for name, kind in [
        ("Paraguay", "country"),
        ("Paris", "city"),
        ("Pardubice", "city"),
        ("Cannes-la-Bocca Paris annex", "city"),  # substring, not prefix
    ]:
        client.post("/admin/areas", json={"name": name, "kind": kind}, headers=admin)

    names = [a["name"] for a in client.get("/areas", params={"q": "par"}, headers=sun).json()]
    # Prefix matches (shorter first) come before the substring-only match.
    assert names[0] == "Paris"  # shortest prefix match
    assert names.index("Paraguay") < names.index("Cannes-la-Bocca Paris annex")
    assert names[-1] == "Cannes-la-Bocca Paris annex"


def test_area_search_exact_match_wins(client, make_user, admin):
    sun = make_user("sun")
    client.post("/admin/areas", json={"name": "Nice city nearby", "kind": "city"}, headers=admin)
    client.post("/admin/areas", json={"name": "Nice", "kind": "city"}, headers=admin)

    names = [a["name"] for a in client.get("/areas", params={"q": "nice"}, headers=sun).json()]
    assert names[0] == "Nice"


def test_area_search_filter_by_kind(client, make_user, admin):
    sun = make_user("sun")
    client.post("/admin/areas", json={"name": "Georgia", "kind": "country"}, headers=admin)
    client.post("/admin/areas", json={"name": "Georgetown", "kind": "city"}, headers=admin)

    cities = client.get("/areas", params={"q": "georg", "kind": "city"}, headers=sun).json()
    assert [a["name"] for a in cities] == ["Georgetown"]


def test_area_search_includes_parent(client, make_user, admin):
    sun = make_user("sun")
    france = client.post(
        "/admin/areas", json={"name": "France", "kind": "country"}, headers=admin
    ).json()
    client.post(
        "/admin/areas",
        json={"name": "Paris", "kind": "city", "parent_id": france["id"]},
        headers=admin,
    )

    paris = client.get("/areas", params={"q": "paris"}, headers=sun).json()[0]
    assert paris["parent_id"] == france["id"]
    assert paris["parent"]["name"] == "France"
    assert paris["parent"]["kind"] == "country"


def test_unknown_area_404s(client, make_user):
    sun = make_user("sun")
    assert client.get(f"/areas/{uuid.uuid4()}", headers=sun).status_code == 404


def test_admin_area_reads_require_admin_token(client, make_user):
    assert client.get("/admin/areas").status_code == 401  # no token
    sun = make_user("sun")  # a user token is authenticated but never admin
    assert client.get("/admin/areas", headers=sun).status_code == 403
    assert client.get(f"/admin/areas/{uuid.uuid4()}", headers=sun).status_code == 403


def test_user_area_reads_reject_service_token(client, admin):
    """The client-facing reads stay user-only; the console uses /admin/areas."""
    assert client.get("/areas", headers=admin).status_code == 401


def test_admin_list_areas_page_shape_and_total(client, admin):
    for name in ("Paris", "Lyon", "Marseille"):
        client.post("/admin/areas", json={"name": name}, headers=admin)

    body = client.get("/admin/areas", headers=admin).json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (50, 0)
    # Alphabetical without q, same as the client search.
    assert [a["name"] for a in body["items"]] == ["Lyon", "Marseille", "Paris"]
    assert "photos" not in body["items"][0]


def test_admin_list_areas_paginates(client, admin):
    for i in range(5):
        client.post("/admin/areas", json={"name": f"Town {i}"}, headers=admin)

    page1 = client.get("/admin/areas?limit=2&offset=0", headers=admin).json()
    page2 = client.get("/admin/areas?limit=2&offset=2", headers=admin).json()
    page3 = client.get("/admin/areas?limit=2&offset=4", headers=admin).json()
    assert page1["total"] == page2["total"] == page3["total"] == 5
    names = [a["name"] for page in (page1, page2, page3) for a in page["items"]]
    assert names == [f"Town {i}" for i in range(5)]
    assert len(page3["items"]) == 1


def test_admin_list_areas_filters_and_ranks(client, admin):
    for name, kind in [
        ("Georgia", "country"),
        ("Georgetown", "city"),
        ("New Georgia", "city"),
        ("Lyon", "city"),
    ]:
        client.post("/admin/areas", json={"name": name, "kind": kind}, headers=admin)

    found = client.get("/admin/areas", params={"q": "georg"}, headers=admin).json()
    # total counts only the matches; ranking matches GET /areas (prefix before substring).
    assert found["total"] == 3
    assert [a["name"] for a in found["items"]] == ["Georgia", "Georgetown", "New Georgia"]

    cities = client.get(
        "/admin/areas", params={"q": "georg", "kind": "city"}, headers=admin
    ).json()
    assert cities["total"] == 2
    assert [a["name"] for a in cities["items"]] == ["Georgetown", "New Georgia"]

    assert client.get("/admin/areas", params={"kind": "city"}, headers=admin).json()["total"] == 3
    # A blank q is no filter.
    assert client.get("/admin/areas", params={"q": "  "}, headers=admin).json()["total"] == 4


def test_admin_read_area(client, admin):
    france = client.post(
        "/admin/areas", json={"name": "France", "kind": "country"}, headers=admin
    ).json()
    paris = client.post(
        "/admin/areas",
        json={"name": "Paris", "kind": "city", "parent_id": france["id"]},
        headers=admin,
    ).json()

    got = client.get(f"/admin/areas/{paris['id']}", headers=admin)
    assert got.status_code == 200
    body = got.json()
    assert body["name"] == "Paris"
    assert body["parent"]["name"] == "France"
    assert body["photos"] == []  # detail view includes the candidate photos
    assert client.get(f"/admin/areas/{uuid.uuid4()}", headers=admin).status_code == 404
