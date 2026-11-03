"""The storage side of trip extensions: a trip can point at the trip it was
extended from and share a group with it. No API makes these links yet; this
pins down what the database allows and what it does when a trip goes."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Trip, TripGroup, User


def _trip(session: Session, owner: User, start: date, end: date, **extra) -> Trip:
    trip = Trip(owner_id=owner.id, title="t", start_date=start, end_date=end, **extra)
    session.add(trip)
    session.flush()
    return trip


def _owner(session: Session) -> User:
    user = User(email=f"{uuid.uuid4()}@example.com", display_name="Sun", password_hash="x")
    session.add(user)
    session.flush()
    return user


def test_standalone_trip_has_no_links(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun)
    assert trip["group_id"] is None
    assert trip["extends_trip_id"] is None


def test_extension_links_and_group_survive_parent_deletion(session):
    sun = _owner(session)
    group = TripGroup()
    session.add(group)
    main = _trip(session, sun, date(2026, 6, 10), date(2026, 6, 20), group=group)
    before = _trip(session, sun, date(2026, 6, 8), date(2026, 6, 10), group=group, extends=main)
    chained = _trip(session, sun, date(2026, 6, 6), date(2026, 6, 8), group=group, extends=before)
    session.commit()

    assert [t.id for t in main.extensions] == [before.id]
    assert chained.extends is before and before.extends is main
    session.expire_all()
    # Reloaded, a group lists its trips by date.
    assert [t.id for t in group.trips] == [chained.id, before.id, main.id]

    # Deleting the middle trip clears the link below it but keeps the cluster.
    session.delete(before)
    session.commit()
    session.expire_all()
    assert chained.extends_trip_id is None
    assert chained.group_id == group.id
    assert {t.id for t in group.trips} == {chained.id, main.id}


def test_parent_link_requires_group_and_is_never_self(session):
    sun = _owner(session)
    main = _trip(session, sun, date(2026, 6, 10), date(2026, 6, 20))

    with pytest.raises(IntegrityError), session.begin_nested():
        _trip(session, sun, date(2026, 6, 8), date(2026, 6, 10), extends=main)

    group = TripGroup()
    session.add(group)
    lone = _trip(session, sun, date(2026, 6, 10), date(2026, 6, 20), group=group)
    with pytest.raises(IntegrityError), session.begin_nested():
        lone.extends_trip_id = lone.id
        session.flush()
    session.expire_all()
    assert session.scalar(select(Trip.extends_trip_id).where(Trip.id == lone.id)) is None


# --- creating extensions through the API ---


def _extend(client, headers, parent, **overrides):
    body = {"title": "Early days", "start_date": "2026-04-07", "end_date": "2026-04-09"}
    return client.post(
        "/trips", json={**body, **overrides, "extends_trip_id": parent["id"]}, headers=headers
    )


def test_extending_a_trip_starts_a_group_the_parent_joins(client, make_user, make_trip):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])
    assert japan["group_id"] is None

    # Any member may extend, not just organizers; the extension is theirs.
    resp = _extend(client, amy, japan)
    assert resp.status_code == 201, resp.text
    early = resp.json()
    assert early["owner_id"] != japan["owner_id"]
    assert early["extends_trip_id"] == japan["id"]
    assert early["group_id"] is not None
    assert [m["email"] for m in early["members"]] == [amy.email]

    # The parent joined the same group; nothing else about it changed.
    parent = client.get(f"/trips/{japan['id']}", headers=sun).json()
    assert parent["group_id"] == early["group_id"]
    assert parent["extends_trip_id"] is None
    assert (parent["start_date"], parent["end_date"]) == (japan["start_date"], japan["end_date"])

    # Sun, on the parent, hears about it; Amy made it and doesn't.
    inbox = client.get("/notifications", headers=sun).json()["items"]
    assert [(n["type"], n["trip_id"], n["data"]) for n in inbox] == [
        ("trip.extended", japan["id"], {"extension_trip_id": early["id"]})
    ]
    assert inbox[0]["body"] == "Amy added Early days, Apr 7 – 9, 2026"
    assert client.get("/notifications", headers=amy).json()["items"] == []


def test_extensions_chain_and_reuse_the_group(client, make_user, make_trip):
    sun, amy, bo = make_user("sun"), make_user("amy"), make_user("bo")
    japan = make_trip(sun)
    early = _extend(client, sun, japan, members=[{"email": amy.email}]).json()
    # Amy is on the extension only; she extends that, and Bo joins hers.
    earlier = _extend(
        client, amy, early, start_date="2026-04-05", end_date="2026-04-07",
        members=[{"email": bo.email}],
    ).json()
    # A second extension off the original lands in the same group.
    after = _extend(client, sun, japan, start_date="2026-04-15", end_date="2026-04-17").json()

    assert earlier["extends_trip_id"] == early["id"]
    assert after["extends_trip_id"] == japan["id"]
    assert {t["group_id"] for t in (early, earlier, after)} == {early["group_id"]}
    # Bo sees only the trip he's on; group membership grants nothing by itself.
    assert [t["id"] for t in client.get("/trips", headers=bo).json()] == [earlier["id"]]


def test_extension_must_be_contiguous_with_its_parent(client, make_user, make_trip):
    sun = make_user("sun")
    japan = make_trip(sun)  # Apr 9 – 15

    gap = _extend(client, sun, japan, start_date="2026-04-01", end_date="2026-04-07")
    assert gap.status_code == 422
    assert "touch or overlap" in gap.json()["detail"]

    # Ending the day before counts as touching; so does sharing days.
    assert _extend(client, sun, japan, start_date="2026-04-06", end_date="2026-04-08").status_code == 201
    assert _extend(client, sun, japan, start_date="2026-04-14", end_date="2026-04-20").status_code == 201


def test_only_members_of_the_parent_may_extend_it(client, make_user, make_trip):
    sun, stranger = make_user("sun"), make_user("stranger")
    japan = make_trip(sun)

    resp = _extend(client, stranger, japan)
    assert resp.status_code == 404
    assert client.get(f"/trips/{japan['id']}", headers=sun).json()["group_id"] is None

    missing = _extend(client, sun, {"id": str(uuid.uuid4())})
    assert missing.status_code == 404
