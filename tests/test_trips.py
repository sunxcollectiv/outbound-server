from tests.conftest import JAPAN_TRIP


def test_trip_validation(client, make_user):
    sun = make_user("sun")

    backwards = {**JAPAN_TRIP, "end_date": "2026-04-01"}
    assert client.post("/trips", json=backwards, headers=sun).status_code == 422

    bad_timezone = {**JAPAN_TRIP, "timezone": "Asia/Tokio"}
    assert client.post("/trips", json=bad_timezone, headers=sun).status_code == 422


def test_trip_is_invisible_to_people_not_on_it(client, make_user, make_trip):
    sun, stranger = make_user("sun"), make_user("stranger")
    trip = make_trip(sun)

    assert client.get(f"/trips/{trip['id']}", headers=stranger).status_code == 404
    assert client.get("/trips", headers=stranger).json() == []
    assert [t["id"] for t in client.get("/trips", headers=sun).json()] == [trip["id"]]


def test_member_roles(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    sean_id = sean.user_id

    # Only the owner manages members.
    added = client.post(
        f"{url}/members", json={"user_id": sean_id, "role": "viewer"}, headers=sun
    )
    assert added.status_code == 201
    assert added.json()["user"]["email"] == "sean@example.com"
    again = client.post(f"{url}/members", json={"user_id": sean_id}, headers=sun)
    assert again.status_code == 409

    # A viewer can read but not change anything.
    assert client.get(url, headers=sean).status_code == 200
    assert [t["id"] for t in client.get("/trips", headers=sean).json()] == [trip["id"]]
    assert client.patch(url, json={"title": "Nope"}, headers=sean).status_code == 403
    assert client.delete(url, headers=sean).status_code == 403

    # Re-add as an editor: can edit, still can't delete or manage members.
    assert client.delete(f"{url}/members/{sean_id}", headers=sun).status_code == 204
    client.post(f"{url}/members", json={"user_id": sean_id, "role": "editor"}, headers=sun)
    renamed = client.patch(url, json={"title": "Japan 2026"}, headers=sean)
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Japan 2026"
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


def test_deleting_a_trip_removes_its_items_and_members(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun)
    url = f"/trips/{trip['id']}"
    client.post(f"{url}/members", json={"user_id": sean.user_id}, headers=sun)
    item = {"kind": "activity", "title": "Dinner", "starts_at": "2026-04-09T18:30:00+09:00"}
    assert client.post(f"{url}/items", json=item, headers=sun).status_code == 201

    assert client.delete(url, headers=sun).status_code == 204
    assert client.get(url, headers=sun).status_code == 404
    assert client.get("/trips", headers=sean).json() == []
