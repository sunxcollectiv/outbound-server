import uuid

from app.services.unsplash import UnsplashPhoto
from tests.conftest import JAPAN_TRIP


def _make_area(client, headers, name, **extra):
    resp = client.post("/areas", json={"name": name, **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _photo(id_: str) -> UnsplashPhoto:
    return UnsplashPhoto(
        id=id_,
        url=f"https://u/{id_}",
        attribution="Jane Doe",
        attribution_url="https://unsplash.com/@jane",
        download_location=f"https://api.unsplash.com/photos/{id_}/download",
        width=4000,
        height=3000,
        color="#262626",
    )


def test_trip_validation(client, make_user):
    sun = make_user("sun")

    backwards = {**JAPAN_TRIP, "end_date": "2026-04-01"}
    assert client.post("/trips", json=backwards, headers=sun).status_code == 422


def test_trip_is_invisible_to_people_not_on_it(client, make_user, make_trip):
    sun, stranger = make_user("sun"), make_user("stranger")
    trip = make_trip(sun)

    assert client.get(f"/trips/{trip['id']}", headers=stranger).status_code == 404
    assert client.get("/trips", headers=stranger).json() == []
    assert [t["id"] for t in client.get("/trips", headers=sun).json()] == [trip["id"]]


def test_member_roles(client, make_user, make_trip):
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    sean_id, amy_id = sean.user_id, amy.user_id
    group_item = {
        "kind": "activity",
        "scope": "group",
        "title": "Ceremony",
        "starts_at": "2026-04-10T14:00:00",
        "start_timezone": "Asia/Tokyo",
    }
    personal_item = {**group_item, "scope": "personal", "title": "Coffee"}

    # The owner is on the member list from the start, as an organizer.
    roster = client.get(f"{url}/members", headers=sun).json()
    assert [(m["user"]["email"], m["role"]) for m in roster] == [("sun@example.com", "organizer")]
    re_added = client.post(f"{url}/members", json={"user_id": sun.user_id}, headers=sun)
    assert re_added.status_code == 409
    assert client.delete(f"{url}/members/{sun.user_id}", headers=sun).status_code == 409

    # Members default to the member role.
    added = client.post(f"{url}/members", json={"user_id": sean_id}, headers=sun)
    assert added.status_code == 201
    assert added.json()["user"]["email"] == "sean@example.com"
    assert added.json()["role"] == "member"
    again = client.post(f"{url}/members", json={"user_id": sean_id}, headers=sun)
    assert again.status_code == 409
    roster = client.get(f"{url}/members", headers=sean).json()
    assert sorted(m["user"]["email"] for m in roster) == ["sean@example.com", "sun@example.com"]

    # A member can read and add their own items, but not change the trip,
    # post group items, or manage members.
    assert client.get(url, headers=sean).status_code == 200
    assert [t["id"] for t in client.get("/trips", headers=sean).json()] == [trip["id"]]
    assert client.post(f"{url}/items", json=personal_item, headers=sean).status_code == 201
    assert client.post(f"{url}/items", json=group_item, headers=sean).status_code == 403
    assert client.patch(url, json={"title": "Nope"}, headers=sean).status_code == 403
    assert client.delete(url, headers=sean).status_code == 403
    denied = client.post(f"{url}/members", json={"user_id": amy_id}, headers=sean)
    assert denied.status_code == 403

    # Re-add as an organizer: can edit the trip, post group items and manage
    # members, still can't delete the trip.
    assert client.delete(f"{url}/members/{sean_id}", headers=sun).status_code == 204
    client.post(f"{url}/members", json={"user_id": sean_id, "role": "organizer"}, headers=sun)
    renamed = client.patch(url, json={"title": "Japan 2026"}, headers=sean)
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Japan 2026"
    assert client.post(f"{url}/items", json=group_item, headers=sean).status_code == 201
    assert client.post(f"{url}/members", json={"user_id": amy_id}, headers=sean).status_code == 201
    assert client.delete(f"{url}/members/{amy_id}", headers=sean).status_code == 204
    assert client.delete(f"{url}/members/{sun.user_id}", headers=sean).status_code == 409
    assert client.delete(url, headers=sean).status_code == 403

    # A member can leave; after that the trip is gone for them.
    assert client.delete(f"{url}/members/{sean_id}", headers=sean).status_code == 204
    assert client.get(url, headers=sean).status_code == 404


def test_update_cannot_put_end_before_start(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)

    response = client.patch(
        f"/trips/{trip['id']}", json={"end_date": "2026-04-01"}, headers=sun
    )
    assert response.status_code == 422


def test_trip_can_be_created_with_areas(client, make_user):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    tokyo = _make_area(client, sun, "Tokyo", kind="city", parent_id=japan["id"])

    created = client.post(
        "/trips",
        json={**JAPAN_TRIP, "area_ids": [tokyo["id"], japan["id"], tokyo["id"]]},  # dupe
        headers=sun,
    )
    assert created.status_code == 201, created.text
    # Resolved to full area objects, deduped, order preserved.
    assert [a["id"] for a in created.json()["areas"]] == [tokyo["id"], japan["id"]]

    # Resolved on both single and list reads, with parent context.
    fetched = client.get(f"/trips/{created.json()['id']}", headers=sun).json()
    assert [a["id"] for a in fetched["areas"]] == [tokyo["id"], japan["id"]]
    by_name = {a["name"]: a for a in fetched["areas"]}
    assert by_name["Tokyo"]["parent"]["name"] == "Japan"
    listed = client.get("/trips", headers=sun).json()[0]
    assert {a["name"] for a in listed["areas"]} == {"Japan", "Tokyo"}


def test_trip_flags_areas_missing_photos_without_fetching(client, make_user, unsplash):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    tokyo = _make_area(client, sun, "Tokyo", kind="city", parent_id=japan["id"])
    unsplash.results = [_photo("a")]  # available, but reads must NOT fetch

    created = client.post(
        "/trips", json={**JAPAN_TRIP, "area_ids": [tokyo["id"], japan["id"]]}, headers=sun
    ).json()

    # Step one: the write comes straight back, flagging both photoless areas and
    # never touching Unsplash.
    assert set(created["areas_missing_photos"]) == {tokyo["id"], japan["id"]}
    assert all(a["default_photo_url"] is None for a in created["areas"])
    assert unsplash.queries == []

    # A read flags the same, still without fetching.
    fetched = client.get(f"/trips/{created['id']}", headers=sun).json()
    assert set(fetched["areas_missing_photos"]) == {tokyo["id"], japan["id"]}
    assert unsplash.queries == []


def test_refresh_photos_fills_covers_and_clears_the_flag(client, make_user, unsplash):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    tokyo = _make_area(client, sun, "Tokyo", kind="city", parent_id=japan["id"])
    trip = client.post(
        "/trips", json={**JAPAN_TRIP, "area_ids": [tokyo["id"], japan["id"]]}, headers=sun
    ).json()

    # Step two: the client calls refresh-photos with the flagged ids.
    unsplash.results = [_photo("a"), _photo("b")]
    refreshed = client.post(
        "/areas/refresh-photos",
        json={"area_ids": trip["areas_missing_photos"]},
        headers=sun,
    )
    assert refreshed.status_code == 200, refreshed.text
    areas = refreshed.json()["areas"]
    assert areas[tokyo["id"]]["default_photo_url"] == "https://u/a"
    assert [p["url"] for p in areas[tokyo["id"]]["photos"]] == ["https://u/a", "https://u/b"]
    # Cities search with their country for context; download pings fired.
    assert "Tokyo Japan" in unsplash.queries
    assert unsplash.downloads

    # The trip no longer flags those areas, and the covers show up.
    fetched = client.get(f"/trips/{trip['id']}", headers=sun).json()
    assert fetched["areas_missing_photos"] == []
    assert {a["default_photo_url"] for a in fetched["areas"]} == {"https://u/a"}


def test_refresh_photos_is_best_effort_when_unsplash_has_nothing(client, make_user, unsplash):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    unsplash.results = []  # no match / rate-limited

    refreshed = client.post("/areas/refresh-photos", json={"area_ids": [japan["id"]]}, headers=sun)
    assert refreshed.status_code == 200
    assert refreshed.json()["areas"][japan["id"]]["photos"] == []
    assert refreshed.json()["areas"][japan["id"]]["default_photo_url"] is None


def test_refresh_photos_omits_unknown_ids(client, make_user, unsplash):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    unsplash.results = [_photo("a")]
    missing = str(uuid.uuid4())

    refreshed = client.post(
        "/areas/refresh-photos", json={"area_ids": [japan["id"], missing]}, headers=sun
    ).json()
    assert set(refreshed["areas"]) == {japan["id"]}


def test_trip_area_ids_must_exist(client, make_user):
    sun = make_user("sun")
    resp = client.post(
        "/trips", json={**JAPAN_TRIP, "area_ids": [str(uuid.uuid4())]}, headers=sun
    )
    assert resp.status_code == 404


def test_patch_replaces_trip_areas(client, make_user, make_trip):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    korea = _make_area(client, sun, "South Korea", kind="country")
    trip = make_trip(sun, area_ids=[japan["id"]])
    url = f"/trips/{trip['id']}"
    assert [a["id"] for a in trip["areas"]] == [japan["id"]]

    # Omitting area_ids leaves them untouched.
    patched = client.patch(url, json={"title": "Trip"}, headers=sun).json()
    assert [a["id"] for a in patched["areas"]] == [japan["id"]]

    # A list replaces the whole set.
    patched = client.patch(url, json={"area_ids": [korea["id"]]}, headers=sun).json()
    assert [a["id"] for a in patched["areas"]] == [korea["id"]]

    # An empty list clears them.
    patched = client.patch(url, json={"area_ids": []}, headers=sun).json()
    assert patched["areas"] == []


def test_trip_get_prunes_deleted_area_ids(client, make_user, make_trip):
    sun = make_user("sun")
    japan = _make_area(client, sun, "Japan", kind="country")
    tokyo = _make_area(client, sun, "Tokyo", kind="city")
    trip = make_trip(sun, area_ids=[japan["id"], tokyo["id"]])

    # Delete an area out from under the trip; the stale id must not surface.
    assert client.delete(f"/areas/{japan['id']}", headers=sun).status_code == 204
    fetched = client.get(f"/trips/{trip['id']}", headers=sun).json()
    assert [a["id"] for a in fetched["areas"]] == [tokyo["id"]]  # japan dropped, order kept
    # Pruned from storage, so a repeat read is consistent.
    again = client.get(f"/trips/{trip['id']}", headers=sun).json()
    assert [a["id"] for a in again["areas"]] == [tokyo["id"]]


def test_deleting_a_trip_removes_its_items_and_members(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    client.post(f"{url}/members", json={"user_id": sean.user_id}, headers=sun)
    item = {
        "kind": "activity",
        "scope": "group",
        "title": "Dinner",
        "starts_at": "2026-04-09T18:30:00",
        "start_timezone": "Asia/Tokyo",
    }
    assert client.post(f"{url}/items", json=item, headers=sun).status_code == 201

    assert client.delete(url, headers=sun).status_code == 204
    assert client.get(url, headers=sun).status_code == 404
    assert client.get("/trips", headers=sean).json() == []
