"""A stay's optional rooms and who's in each (trip_item_lodging_rooms +
trip_item_participants.room_id)."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import TripItemLodgingRoom, TripItemParticipant

STAY = {
    "kind": "lodging",
    "title": "Hotel",
    "starts_at": "2026-04-10T15:00:00",
    "start_timezone": "Asia/Tokyo",
    "ends_at": "2026-04-12T11:00:00",
}


@pytest.fixture
def family(client, make_user, make_trip):
    """Sun organizes; Sean and Amy are on the trip, and Grandma has no account."""
    sun, sean, amy = make_user("sun"), make_user("sean"), make_user("amy")
    trip = make_trip(
        sun, members=[{"email": sean.email}, {"email": amy.email}, {"email": "grandma@example.com"}]
    )
    ids = {m["email"].split("@")[0]: m["id"] for m in trip["members"]}
    return {"url": f"/trips/{trip['id']}/items", "sun": sun, "sean": sean, "ids": ids}


def _rooms_by_label(item):
    return {room["label"]: room for room in item["rooms"]}


def _where(item):
    """member id -> room id, from the participants."""
    return {p["member"]["id"]: p["room_id"] for p in item["participants"]}


def test_a_stay_needs_no_rooms(client, family):
    url, sun = family["url"], family["sun"]
    stay = client.post(url, json=STAY, headers=sun).json()
    assert stay["rooms"] == []
    assert all(p["room_id"] is None for p in stay["participants"])
    # Editing the stay never asks about rooms.
    edited = client.patch(f"{url}/{stay['id']}", json={"title": "Mom's place"}, headers=sun)
    assert edited.status_code == 200 and edited.json()["rooms"] == []


def test_rooms_round_trip(client, family):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    created = client.post(
        url,
        json={
            **STAY,
            "participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}],
            "rooms": [
                {"label": "Room 1", "room_type": "King", "guests": 2,
                 "occupants": [ids["sun"], ids["sean"]]},
                # Amy isn't a participant yet: putting her in a room adds her.
                {"label": "Room 2", "confirmation_number": "R2-99", "occupants": [ids["amy"]]},
            ],
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    stay = created.json()
    rooms = _rooms_by_label(stay)
    one, two = rooms["Room 1"], rooms["Room 2"]
    assert (one["position"], one["room_type"], one["guests"]) == (0, "King", 2)
    assert (two["position"], two["confirmation_number"]) == (1, "R2-99")
    assert sorted(one["occupants"]) == sorted([ids["sun"], ids["sean"]])
    assert two["occupants"] == [ids["amy"]]
    assert _where(stay) == {ids["sun"]: one["id"], ids["sean"]: one["id"], ids["amy"]: two["id"]}

    item_url = f"{url}/{stay['id']}"
    assert client.get(item_url, headers=sun).json()["rooms"] == stay["rooms"]

    # Edits that don't mention rooms leave them, and everyone's place, alone.
    renamed = client.patch(item_url, json={"title": "The Hotel"}, headers=sun).json()
    assert renamed["rooms"] == stay["rooms"] and _where(renamed) == _where(stay)

    # Sending rooms replaces them: Room 1 kept (same id, now just Sun), Room 2
    # dropped, a new room for Sean and Amy.
    moved = client.patch(
        item_url,
        json={"rooms": [
            {"id": one["id"], "label": "Room 1", "room_number": "1204", "occupants": [ids["sun"]]},
            {"label": "Kids", "occupants": [ids["sean"], ids["amy"]]},
        ]},
        headers=sun,
    )
    assert moved.status_code == 200, moved.text
    after = _rooms_by_label(moved.json())
    assert set(after) == {"Room 1", "Kids"}
    assert after["Room 1"]["id"] == one["id"] and after["Room 1"]["room_number"] == "1204"
    # Fields left out of a kept room are cleared, like any replace.
    assert after["Room 1"]["room_type"] is None
    kids = after["Kids"]["id"]
    assert _where(moved.json()) == {ids["sun"]: one["id"], ids["sean"]: kids, ids["amy"]: kids}

    # Dropping Amy from the stay takes her out of her room too.
    dropped = client.patch(
        item_url,
        json={"participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}]},
        headers=sun,
    ).json()
    assert _rooms_by_label(dropped)["Kids"]["occupants"] == [ids["sean"]]

    # [] clears the rooms; everyone stays on the stay, in no room.
    cleared = client.patch(item_url, json={"rooms": []}, headers=sun).json()
    assert cleared["rooms"] == []
    assert _where(cleared) == {ids["sun"]: None, ids["sean"]: None}


def test_leaving_lodging_drops_the_rooms(client, family, session):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    stay = client.post(
        url, json={**STAY, "rooms": [{"label": "Room 1", "occupants": [ids["sun"]]}]}, headers=sun
    ).json()
    moved = client.patch(f"{url}/{stay['id']}", json={"kind": "activity"}, headers=sun).json()
    assert moved["rooms"] == [] and _where(moved) == {ids["sun"]: None}
    session.expire_all()
    assert session.scalars(
        select(TripItemLodgingRoom).where(TripItemLodgingRoom.trip_item_id == uuid.UUID(stay["id"]))
    ).all() == []


def test_room_validation(client, family, make_user, make_trip):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    room = {"label": "Room 1"}

    # Only stays have rooms.
    assert client.post(
        url, json={**STAY, "kind": "activity", "rooms": [room]}, headers=sun
    ).status_code == 422
    activity = client.post(url, json={**STAY, "kind": "activity"}, headers=sun).json()
    assert client.patch(
        f"{url}/{activity['id']}", json={"rooms": [room]}, headers=sun
    ).status_code == 422

    # A member is in one room at most, and listed once.
    assert client.post(url, json={**STAY, "rooms": [
        {"occupants": [ids["sean"]]}, {"occupants": [ids["sean"]]},
    ]}, headers=sun).status_code == 422
    assert client.post(
        url, json={**STAY, "rooms": [{"occupants": [ids["sean"], ids["sean"]]}]}, headers=sun
    ).status_code == 422

    # Occupants must be on the trip.
    stranger = make_trip(make_user("zed"))["members"][0]["id"]
    assert client.post(
        url, json={**STAY, "rooms": [{"occupants": [stranger]}]}, headers=sun
    ).status_code == 422

    # A new stay's rooms can't claim ids; an edit can only keep this stay's rooms.
    assert client.post(
        url, json={**STAY, "rooms": [{"id": str(uuid.uuid4())}]}, headers=sun
    ).status_code == 422
    stay = client.post(url, json={**STAY, "rooms": [room]}, headers=sun).json()
    other = client.post(url, json={**STAY, "rooms": [room]}, headers=sun).json()
    stolen = other["rooms"][0]["id"]
    assert client.patch(
        f"{url}/{stay['id']}", json={"rooms": [{"id": stolen}]}, headers=sun
    ).status_code == 422

    for bad in ({"guests": 0}, {"label": ""}, {"nope": 1}):
        assert client.post(url, json={**STAY, "rooms": [bad]}, headers=sun).status_code == 422, bad


def test_the_database_keeps_a_participant_in_their_own_stay(client, family, session):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    stay = client.post(
        url, json={**STAY, "rooms": [{"label": "A", "occupants": [ids["sun"]]}]}, headers=sun
    ).json()
    other = client.post(url, json={**STAY, "rooms": [{"label": "B"}]}, headers=sun).json()
    room_a, room_b = stay["rooms"][0]["id"], other["rooms"][0]["id"]

    # Pointing a participant at another stay's room fails. The check is
    # deferred to commit, so force it now inside a savepoint.
    nested = session.begin_nested()
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE trip_item_participants SET room_id = :room WHERE item_id = :item"),
            {"room": room_b, "item": stay["id"]},
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    nested.rollback()
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))

    # Deleting a room straight in the database unassigns, never removes, people.
    session.execute(text("DELETE FROM trip_item_lodging_rooms WHERE id = :id"), {"id": room_a})
    session.expire_all()
    reread = client.get(f"{url}/{stay['id']}", headers=sun).json()
    assert reread["rooms"] == [] and _where(reread) == {ids["sun"]: None}


def test_deleting_a_stay_takes_its_rooms(client, family, session):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    stay = client.post(
        url, json={**STAY, "rooms": [{"occupants": [ids["sun"]]}]}, headers=sun
    ).json()
    assert client.delete(f"{url}/{stay['id']}", headers=sun).status_code == 204
    session.expire_all()
    item_id = uuid.UUID(stay["id"])
    assert session.scalars(
        select(TripItemLodgingRoom).where(TripItemLodgingRoom.trip_item_id == item_id)
    ).all() == []
    assert session.scalars(
        select(TripItemParticipant).where(TripItemParticipant.item_id == item_id)
    ).all() == []


def test_closed_stay_hides_its_rooms(client, family):
    url, sun, sean, ids = family["url"], family["sun"], family["sean"], family["ids"]
    client.post(
        url,
        json={**STAY, "visibility": "closed", "participants": [{"member_id": ids["sun"]}],
              "rooms": [{"label": "Suite", "occupants": [ids["sun"]]}]},
        headers=sun,
    )
    [seen] = client.get(url, headers=sean).json()
    assert seen["redacted"] is True and seen["rooms"] == []


def test_moving_rooms_counts_as_a_change(client, family):
    url, sun, sean, ids = family["url"], family["sun"], family["sean"], family["ids"]
    stay = client.post(
        url,
        json={**STAY, "participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}],
              "rooms": [{"label": "A", "occupants": [ids["sun"], ids["sean"]]}, {"label": "B"}]},
        headers=sun,
    ).json()
    a, b = stay["rooms"][0]["id"], stay["rooms"][1]["id"]
    client.patch(
        f"{url}/{stay['id']}",
        json={"notify_members": True, "rooms": [
            {"id": a, "label": "A", "occupants": [ids["sun"]]},
            {"id": b, "label": "B", "occupants": [ids["sean"]]},
        ]},
        headers=sun,
    )
    inbox = client.get("/notifications", headers=sean).json()
    items = inbox["items"] if isinstance(inbox, dict) else inbox
    assert any("details" in (n.get("body") or "") for n in items), items


def test_taking_someone_out_of_a_room_leaves_them_unassigned(client, family):
    url, sun, ids = family["url"], family["sun"], family["ids"]
    stay = client.post(
        url,
        json={**STAY, "rooms": [{"label": "A", "occupants": [ids["sun"], ids["sean"]]}]},
        headers=sun,
    ).json()
    room = stay["rooms"][0]["id"]
    # Putting Sean in a room made him a participant.
    assert _where(stay) == {ids["sun"]: room, ids["sean"]: room}

    out = client.patch(
        f"{url}/{stay['id']}",
        json={"rooms": [{"id": room, "label": "A", "occupants": [ids["sun"]]}]},
        headers=sun,
    ).json()
    # Still on the stay, just in no room.
    assert _where(out) == {ids["sun"]: room, ids["sean"]: None}
    assert out["rooms"][0]["occupants"] == [ids["sun"]]


def test_a_draft_saves_everything_in_one_call_with_one_notice(client, family, make_user):
    """Drafts: the client gathers many edits — times, lodging details, rooms
    (with their types and guests), who's in each, participants — and saves them
    in a single POST/PATCH, telling members once about all of it."""
    url, sun, sean, ids = family["url"], family["sun"], family["sean"], family["ids"]

    def inbox(headers):
        return [n["body"] for n in client.get("/notifications", headers=headers).json()["items"]]

    created = client.post(
        url,
        json={
            **STAY,
            "title": "Ryokan",
            "confirmation_number": "TW8812",
            "lodging": {"property_type": "hotel", "phone": "+81 75-000-0000"},
            "participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]}],
            "rooms": [
                {"label": "Room 1", "room_type": "Japanese-style", "guests": 2,
                 "occupants": [ids["sun"], ids["sean"]]},
            ],
            "notify_members": True,
        },
        headers=sun,
    )
    assert created.status_code == 201, created.text
    stay = created.json()
    assert len(inbox(sean)) == 1
    room = stay["rooms"][0]

    edited = client.patch(
        f"{url}/{stay['id']}",
        json={
            "title": "Tawaraya",
            "starts_at": "2026-04-10T16:00:00",
            "lodging": {"property_type": "hotel", "phone": "+81 75-111-1111",
                        "free_cancellation_until": "2026-04-07T18:00:00"},
            "participants": [{"member_id": ids["sun"]}, {"member_id": ids["sean"]},
                             {"member_id": ids["amy"]}],
            "rooms": [
                {"id": room["id"], "label": "Room 1", "room_type": "Japanese-style",
                 "guests": 1, "occupants": [ids["sun"]]},
                {"label": "Room 2", "room_type": "Twin", "guests": 2,
                 "occupants": [ids["sean"], ids["amy"]]},
            ],
            "notify_members": True,
        },
        headers=sun,
    )
    assert edited.status_code == 200, edited.text
    after = edited.json()
    assert after["title"] == "Tawaraya" and after["starts_at"] == "2026-04-10T16:00:00"
    assert after["lodging"]["phone"] == "+81 75-111-1111"
    assert [(r["room_type"], r["guests"]) for r in after["rooms"]] == [
        ("Japanese-style", 1), ("Twin", 2)
    ]
    rooms = {r["label"]: r["id"] for r in after["rooms"]}
    assert _where(after) == {ids["sun"]: rooms["Room 1"], ids["sean"]: rooms["Room 2"],
                             ids["amy"]: rooms["Room 2"]}

    # One notice for the whole save, naming everything that changed.
    bodies = inbox(sean)
    assert len(bodies) == 2
    update = next(b for b in bodies if "updated" in b)
    assert update == "Sun updated the name, time, participants and details of Tawaraya", update
