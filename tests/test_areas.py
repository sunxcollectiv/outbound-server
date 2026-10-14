import uuid


def test_area_crud(client, make_user):
    sun = make_user("sun")

    created = client.post(
        "/areas",
        json={"name": "Paris", "photo_url": "https://x/p.jpg"},
        headers=sun,
    )
    assert created.status_code == 201, created.text
    area = created.json()
    assert area["name"] == "Paris"
    assert area["photo_url"] == "https://x/p.jpg"

    url = f"/areas/{area['id']}"
    patched = client.patch(url, json={"name": "Paris, FR"}, headers=sun)
    assert patched.status_code == 200
    assert patched.json()["name"] == "Paris, FR"
    assert client.patch(url, json={"name": None}, headers=sun).status_code == 422

    assert client.get(url, headers=sun).json()["name"] == "Paris, FR"
    assert client.delete(url, headers=sun).status_code == 204
    assert client.get(url, headers=sun).status_code == 404


def test_area_search_by_name(client, make_user):
    sun = make_user("sun")
    for name in ("Paris", "Lyon", "Marseille"):
        client.post("/areas", json={"name": name}, headers=sun)

    found = client.get("/areas", params={"q": "lyo"}, headers=sun)
    assert [area["name"] for area in found.json()] == ["Lyon"]


def test_unknown_area_404s(client, make_user):
    sun = make_user("sun")
    assert client.get(f"/areas/{uuid.uuid4()}", headers=sun).status_code == 404
