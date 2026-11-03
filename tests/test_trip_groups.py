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
