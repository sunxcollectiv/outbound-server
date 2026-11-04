"""The ledger as its owner reads it (GET /credits): paged newest first, with
the balance after each row, a live link to the trip while they're on it, and
who's on the other side of refunds. Prices as in test_credits.py: a 7-day
trip is 800, a 6-day one 720, a 9-day one 960."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import CreditEntry, CreditEntryKind
from tests.test_credits import FUTURE_TRIP


def _page(client, headers, **params):
    response = client.get("/credits", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _party(headers):
    return {"id": headers.user_id, "display_name": headers.email.split("@")[0].title()}


def test_pages_carry_the_running_balance(client, make_user, make_trip, grant_credits):
    sun = make_user("sun", credits=0)
    grant_credits(sun, 1000)
    trip = make_trip(sun, **FUTURE_TRIP)  # -800
    client.patch(f"/trips/{trip['id']}", json={"end_date": "2026-12-06"}, headers=sun)  # +80 back
    grant_credits(sun, 500)

    first = _page(client, sun, limit=3)
    assert first["balance_cents"] == 780
    assert [(e["kind"], e["amount_cents"], e["balance_after_cents"]) for e in first["entries"]] == [
        ("grant", 500, 780),
        ("trip_refund", 80, 280),
        ("trip_charge", -800, 200),
    ]
    assert first["next_cursor"] == first["entries"][-1]["id"]

    rest = _page(client, sun, limit=3, before=first["next_cursor"])
    assert rest["balance_cents"] == 780  # the whole balance, not the page's
    assert [(e["kind"], e["balance_after_cents"]) for e in rest["entries"]] == [("grant", 1000)]
    assert rest["next_cursor"] is None


def test_cursor_must_be_one_of_the_readers_rows(client, make_user):
    sun, amy = make_user("sun"), make_user("amy")
    amys_row = _page(client, amy)["entries"][0]["id"]
    for cursor in (amys_row, str(uuid.uuid4())):
        response = client.get("/credits", params={"before": cursor}, headers=sun)
        assert response.status_code == 422
        assert response.json()["detail"] == "Unknown cursor"
    assert client.get("/credits", params={"limit": 0}, headers=sun).status_code == 422
    assert client.get("/credits", params={"limit": 101}, headers=sun).status_code == 422


def test_rows_link_to_the_trip_while_it_exists(client, make_user, make_trip):
    sun = make_user("sun")
    trip = make_trip(sun, **FUTURE_TRIP)
    client.patch(f"/trips/{trip['id']}", json={"title": "Lisbon & Porto"}, headers=sun)

    charge = _page(client, sun)["entries"][0]
    # The link shows the trip as it is now; the row's title keeps what it was.
    assert charge["trip"] == {
        "id": trip["id"],
        "title": "Lisbon & Porto",
        "start_date": "2026-12-01",
        "end_date": "2026-12-07",
    }
    assert charge["title"] == "Unlocked Lisbon"

    assert client.delete(f"/trips/{trip['id']}", headers=sun).status_code == 204
    refund, charge = _page(client, sun)["entries"][:2]
    # Each row took the title the trip had when it was written.
    assert (refund["trip"], refund["trip_id"], refund["title"]) == (
        None, trip["id"], "Refund: Lisbon & Porto",
    )
    assert (charge["trip"], charge["trip_id"]) == (None, trip["id"])


def test_no_link_once_the_reader_has_left(client, make_user, make_trip):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, **FUTURE_TRIP, members=[{"email": sean.email, "role": "organizer"}])
    client.patch(f"/trips/{trip['id']}", json={"end_date": "2026-12-09"}, headers=sean)  # Sean pays 160
    assert _page(client, sean)["entries"][0]["trip"]["id"] == trip["id"]

    url = f"/trips/{trip['id']}/members"
    me = next(m for m in client.get(url, headers=sean).json() if m["email"] == sean.email)
    assert client.delete(f"{url}/{me['id']}", headers=sean).status_code == 204
    charge = _page(client, sean)["entries"][0]
    assert (charge["kind"], charge["trip"], charge["trip_id"]) == ("trip_charge", None, trip["id"])


def test_refunds_show_whose_payment_they_came_from(client, make_user, make_trip, admin):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, **FUTURE_TRIP, members=[{"email": sean.email, "role": "organizer"}])
    url = f"/trips/{trip['id']}"
    client.patch(url, json={"end_date": "2026-12-09"}, headers=sean)  # Sean pays 160
    # Down to 6 days: 240 back to Sun, all of Sean's 160 first, then 80 of Sun's.
    client.patch(url, json={"end_date": "2026-12-06"}, headers=sean)

    # Sean sees his payment went back, and to whom.
    seans_charge = _page(client, sean)["entries"][0]
    assert (seans_charge["kind"], seans_charge["amount_cents"]) == ("trip_charge", -160)
    assert seans_charge["refunded_cents"] == 160
    assert seans_charge["refunded_to"] == [_party(sun)]
    assert (seans_charge["refund_of_id"], seans_charge["refund_of_payer"]) == (None, None)

    # Sun sees whose payment each refund came out of.
    small, large, suns_charge = _page(client, sun)["entries"][:3]
    assert (small["amount_cents"], small["refund_of_id"], small["refund_of_payer"]) == (
        80, suns_charge["id"], _party(sun),
    )
    assert (large["amount_cents"], large["refund_of_id"], large["refund_of_payer"]) == (
        160, seans_charge["id"], _party(sean),
    )
    assert (large["refunded_cents"], large["refunded_to"]) == (0, [])
    assert (suns_charge["refunded_cents"], suns_charge["refunded_to"]) == (80, [_party(sun)])

    # The admin view is the same page, from Sun's side.
    as_admin = client.get(f"/admin/users/{sun.user_id}/credits", params={"limit": 2}, headers=admin)
    assert as_admin.status_code == 200
    assert as_admin.json() == _page(client, sun, limit=2)


def test_a_grant_answers_with_its_statement_row(client, make_user, admin):
    sun = make_user("sun", credits=0)
    url = "/admin/credits/grants"
    client.post(url, json={"user_id": sun.user_id, "amount_cents": 300}, headers=admin)
    entry = client.post(url, json={"user_id": sun.user_id, "amount_cents": 200}, headers=admin).json()["entry"]
    assert (entry["amount_cents"], entry["balance_after_cents"], entry["trip"]) == (200, 500, None)
    assert entry == _page(client, sun)["entries"][0]


# --- the chain: the database holds every balance to account ---


def _row(user_id, amount, previous=None, after=None, kind="grant"):
    prev_balance = previous.balance_after_cents if previous is not None else None
    return CreditEntry(
        user_id=uuid.UUID(str(user_id)),
        kind=CreditEntryKind(kind),
        amount_cents=amount,
        previous_id=previous.id if previous is not None else None,
        previous_balance_cents=prev_balance,
        balance_after_cents=after if after is not None else (prev_balance or 0) + amount,
    )


def _rejected(session, row):
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(row)
        session.flush()


def test_rows_chain_onto_the_one_before(make_user, make_trip, session, client):
    sun = make_user("sun")
    trip = make_trip(sun, **FUTURE_TRIP)
    client.patch(f"/trips/{trip['id']}", json={"end_date": "2026-12-06"}, headers=sun)
    rows = session.scalars(
        select(CreditEntry).where(CreditEntry.user_id == sun.user_id).order_by(CreditEntry.seq)
    ).all()
    assert [r.previous_id for r in rows] == [None] + [r.id for r in rows[:-1]]
    assert [r.balance_after_cents - r.amount_cents for r in rows] == [0] + [
        r.balance_after_cents for r in rows[:-1]
    ]


def test_the_database_refuses_a_broken_chain(make_user, session):
    sun, amy = make_user("sun"), make_user("amy")
    last = session.scalar(select(CreditEntry).where(CreditEntry.user_id == sun.user_id))
    amys = session.scalar(select(CreditEntry).where(CreditEntry.user_id == amy.user_id))

    _rejected(session, _row(sun.user_id, 100))  # a second first row: two writers saw nothing
    _rejected(session, _row(sun.user_id, 100, last, after=last.balance_after_cents + 99))  # doesn't add up
    _rejected(session, _row(sun.user_id, 100, amys))  # built on someone else's row
    stale = _row(sun.user_id, 100, last)
    stale.previous_balance_cents -= 1
    stale.balance_after_cents -= 1
    _rejected(session, stale)  # quotes a balance the previous row doesn't have
    overdrawn = -(last.balance_after_cents + 1)
    _rejected(session, _row(sun.user_id, overdrawn, last, kind="trip_charge"))  # below zero

    # Two writes that both built on the same row: the second loses.
    session.add(_row(sun.user_id, 100, last))
    session.flush()
    _rejected(session, _row(sun.user_id, 200, last))
