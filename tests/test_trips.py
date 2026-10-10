from tests.conftest import JAPAN_TRIP


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
        "starts_at": "2026-04-10T14:00:00+09:00",
    }
    personal_item = {**group_item, "scope": "personal", "title": "Coffee"}

    # Members default to the member role.
    added = client.post(f"{url}/members", json={"user_id": sean_id}, headers=sun)
    assert added.status_code == 201
    assert added.json()["user"]["email"] == "sean@example.com"
    assert added.json()["role"] == "member"
    again = client.post(f"{url}/members", json={"user_id": sean_id}, headers=sun)
    assert again.status_code == 409

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
    item = {
        "kind": "activity",
        "scope": "group",
        "title": "Dinner",
        "starts_at": "2026-04-09T18:30:00+09:00",
    }
    assert client.post(f"{url}/items", json=item, headers=sun).status_code == 201

    assert client.delete(url, headers=sun).status_code == 204
    assert client.get(url, headers=sun).status_code == 404
    assert client.get("/trips", headers=sean).json() == []
