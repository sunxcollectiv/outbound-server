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


def test_area_search_ignores_accents(client, make_user, admin):
    sun = make_user("sun")
    for name in ("Kraków", "Montréal", "Łódź", "Lyon"):
        client.post("/admin/areas", json={"name": name}, headers=admin)

    def search(q: str) -> list[str]:
        return [a["name"] for a in client.get("/areas", params={"q": q}, headers=sun).json()]

    # Typed without accents: the whole name, a prefix, and a mid-name substring.
    assert search("krakow") == ["Kraków"]
    assert search("montre") == ["Montréal"]
    assert search("treal") == ["Montréal"]
    # Typed with them (in any case) still works, for the same three.
    assert search("Kraków") == ["Kraków"]
    assert search("MONTRÉ") == ["Montréal"]
    assert search("tréal") == ["Montréal"]
    # Letters that aren't a base letter plus a mark fold too.
    assert search("lodz") == ["Łódź"]
    # An accent in the term doesn't stop it matching a plain name.
    assert search("lyón") == ["Lyon"]


def test_area_search_ranks_accented_names_like_plain_ones(client, make_user, admin, session):
    from sqlalchemy import select

    from app.api.areas import _area_search
    from app.models import Area

    sun = make_user("sun")
    for name in ("Vélez-Málaga", "Malagasy Coast", "Málaga", "Kemal", "Lyon"):
        client.post("/admin/areas", json={"name": name, "kind": "city"}, headers=admin)

    def search(q: str) -> list[str]:
        return [a["name"] for a in client.get("/areas", params={"q": q}, headers=sun).json()]

    # Exact, then prefix, then substring, whether or not the accent is typed.
    ranked = ["Málaga", "Malagasy Coast", "Vélez-Málaga"]
    assert search("malaga") == ranked
    assert search("málaga") == ranked
    assert search("MÁLAGA") == ranked
    # "Málaga" is a prefix match for "mal", so it beats the shorter "Kemal",
    # which only contains it.
    assert search("mal") == ["Málaga", "Malagasy Coast", "Kemal", "Vélez-Málaga"]

    # The tier each name lands in for "malaga": 0 exact, 1 prefix, 2 substring.
    # (Ordering alone can't tell an exact match from the shortest prefix match.)
    filters, order_by = _area_search("malaga", None)
    tiers = dict(session.execute(select(Area.name, order_by[0]).where(*filters)).all())
    assert tiers == {"Málaga": 0, "Malagasy Coast": 1, "Vélez-Málaga": 2}


def test_area_search_takes_wildcard_characters_literally(client, make_user, admin):
    sun = make_user("sun")
    for name in ("100% Agave", "1000 Islands", "Rue_Basse", "Rue Basse"):
        client.post("/admin/areas", json={"name": name}, headers=admin)

    def search(q: str) -> list[str]:
        return [a["name"] for a in client.get("/areas", params={"q": q}, headers=sun).json()]

    assert search("100%") == ["100% Agave"]
    assert search("%") == ["100% Agave"]
    assert search("rue_b") == ["Rue_Basse"]
    assert search("_") == ["Rue_Basse"]
    # A character the accent-folding itself turns into "%" is no wildcard either.
    assert search("％") == ["100% Agave"]


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


def test_admin_list_areas_ignores_accents(client, admin):
    for name in ("Córdoba", "Cordoba Province", "Nueva Córdoba", "Lyon"):
        client.post("/admin/areas", json={"name": name, "kind": "city"}, headers=admin)

    # Same folding as GET /areas, and the total counts the accented matches too.
    for q in ("cordoba", "córdoba"):
        found = client.get("/admin/areas", params={"q": q}, headers=admin).json()
        assert found["total"] == 3
        assert [a["name"] for a in found["items"]] == [
            "Córdoba",
            "Cordoba Province",
            "Nueva Córdoba",
        ]


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
