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


# --- reading a group: the trip list and the merged itinerary ---


def _list(client, headers):
    response = client.get("/trips", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _itinerary(client, headers, trip):
    response = client.get(f"/trips/{trip['id']}/itinerary", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _add(client, headers, trip, title, starts_at, **fields):
    payload = {"kind": "activity", "title": title, "starts_at": starts_at, **fields}
    response = client.post(f"/trips/{trip['id']}/items", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_trip_list_nests_a_journey_under_its_lead_trip(client, make_user, make_trip):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun)  # Apr 9 – 15
    early = _extend(client, sun, japan, members=[{"email": amy.email}]).json()  # Apr 7 – 9
    after = _extend(client, sun, japan, start_date="2026-04-15", end_date="2026-04-17").json()
    korea = make_trip(sun, title="Korea", start_date="2026-04-01", end_date="2026-04-03")

    entries = _list(client, sun)
    assert [e["id"] for e in entries] == [korea["id"], japan["id"]]
    assert entries[0]["extensions"] == []
    assert (entries[0]["span_start_date"], entries[0]["span_end_date"]) == (
        "2026-04-01",
        "2026-04-03",
    )
    journey = entries[1]
    assert [t["id"] for t in journey["extensions"]] == [early["id"], after["id"]]
    assert (journey["span_start_date"], journey["span_end_date"]) == ("2026-04-07", "2026-04-17")
    assert journey["extensions"][0]["members"][1]["email"] == amy.email

    # Amy is on the extension only: it stands alone for her, still naming its parent.
    [hers] = _list(client, amy)
    assert hers["id"] == early["id"] and hers["extensions"] == []
    assert hers["extends_trip_id"] == japan["id"]


def test_trip_list_nests_across_a_trip_the_caller_is_not_on(client, make_user, make_trip, session):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])  # Apr 9 – 15
    early = _extend(client, amy, japan).json()  # Apr 7 – 9, Amy's
    earlier = _extend(
        client, amy, early, start_date="2026-04-05", end_date="2026-04-07",
        members=[{"email": sun.email}],
    ).json()

    # Sun is on japan and earlier but not early: earlier still descends from japan.
    [entry] = _list(client, sun)
    assert entry["id"] == japan["id"]
    assert [t["id"] for t in entry["extensions"]] == [earlier["id"]]

    # With the middle trip gone the two stay one journey (they share a group).
    assert client.delete(f"/trips/{early['id']}", headers=amy).status_code == 204
    [entry] = _list(client, sun)
    assert [entry["id"], *(t["id"] for t in entry["extensions"])] == [earlier["id"], japan["id"]]


def test_itinerary_merges_the_callers_trips_in_the_group(client, make_user, make_trip):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])  # Apr 9 – 15
    early = _extend(client, sun, japan).json()  # Apr 7 – 9, overlapping on the 9th
    _add(client, sun, early, "Osaka castle", "2026-04-08T10:00:00")
    _add(client, sun, japan, "Land at HND", "2026-04-09T11:00:00")
    _add(client, sun, early, "Shinkansen to Tokyo", "2026-04-09T08:00:00")

    merged = _itinerary(client, sun, japan)
    assert merged == _itinerary(client, sun, early) | {"trip": merged["trip"]}
    assert merged["trip"]["id"] == japan["id"]
    assert [t["id"] for t in merged["trips"]] == [early["id"], japan["id"]]

    days = {day["date"]: day for day in merged["days"]}
    assert list(days) == [f"2026-04-{d:02d}" for d in range(7, 16)]
    assert [day["day_number"] for day in merged["days"]] == list(range(1, 10))
    assert days["2026-04-07"]["trip_id"] == early["id"]
    # The shared day belongs to the parent, and says it's shared.
    assert days["2026-04-09"]["trip_id"] == japan["id"]
    assert days["2026-04-09"]["trip_ids"] == [early["id"], japan["id"]]
    assert days["2026-04-10"]["trip_ids"] == [japan["id"]]
    assert [(e["item"]["title"], e["item"]["trip_id"]) for e in days["2026-04-09"]["items"]] == [
        ("Shinkansen to Tokyo", early["id"]),
        ("Land at HND", japan["id"]),
    ]

    # Amy isn't on the extension: she gets the parent alone, as before.
    hers = _itinerary(client, amy, japan)
    assert [t["id"] for t in hers["trips"]] == [japan["id"]]
    assert (hers["days"][0]["date"], hers["days"][0]["day_number"]) == ("2026-04-09", 1)
    assert [e["item"]["title"] for e in hers["days"][0]["items"]] == ["Land at HND"]


def test_itinerary_leaves_a_gap_day_unowned(client, make_user, make_trip):
    sun = make_user("sun")
    japan = make_trip(sun)  # Apr 9 – 15
    early = _extend(client, sun, japan, start_date="2026-04-06", end_date="2026-04-08").json()
    earlier = _extend(client, sun, early, start_date="2026-04-04", end_date="2026-04-06").json()
    assert client.delete(f"/trips/{early['id']}", headers=sun).status_code == 204

    days = {day["date"]: day for day in _itinerary(client, sun, japan)["days"]}
    assert days["2026-04-06"]["trip_id"] == earlier["id"]
    assert (days["2026-04-07"]["trip_id"], days["2026-04-07"]["trip_ids"]) == (None, [])
    assert days["2026-04-09"]["day_number"] == 6


def test_merged_itinerary_keeps_each_trips_visibility(client, make_user, make_trip):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])
    early = _extend(client, sun, japan, members=[{"email": amy.email}]).json()
    amy_on_early = next(m["id"] for m in early["members"] if m["email"] == amy.email)

    _add(client, sun, early, "Sun's spa", "2026-04-08T09:00:00", visibility="private")
    _add(
        client, sun, early, "Amy's birthday", "2026-04-08T10:00:00", visibility="private",
        participants=[{"member_id": amy_on_early}],
    )
    _add(client, sun, early, "Sun's dinner", "2026-04-08T19:00:00", visibility="closed")
    _add(
        client, sun, japan, "Ramen", "2026-04-10T12:00:00", visibility="closed",
        participants=[{"member_id": next(
            m["id"] for m in japan["members"] if m["email"] == amy.email
        )}],
    )

    days = {day["date"]: day for day in _itinerary(client, amy, japan)["days"]}
    shown = {e["item"]["display_title"]: e["item"]["redacted"] for e in days["2026-04-08"]["items"]}
    assert shown == {"Amy's birthday": False, "Sun's dinner": True}
    # Her own member row on japan makes her a participant there, unredacted.
    assert [(e["item"]["title"], e["item"]["redacted"]) for e in days["2026-04-10"]["items"]] == [
        ("Ramen", False)
    ]
