"""Paying for trips: quotes, charges on create and edit, refunds inside the
window, and where credits come from. Rates are pinned in test_pricing.py;
here a 7-day trip is $8.00 (800) and a sixth person on it is $1.40 (140)."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models import CreditEntry
from app.services import pricing
from app.services.pricing import Bracket, Schedule
from tests.conftest import JAPAN_TRIP, STARTING_CREDITS

# JAPAN_TRIP is long over; this one is a month out, so refunds are still on.
FUTURE_TRIP = {"title": "Lisbon", "start_date": "2026-12-01", "end_date": "2026-12-07"}
CUTOFF = datetime(2026, 12, 2, tzinfo=UTC)  # 24h after Lisbon starts (UTC midnight)


def _charges(credits_of, headers, kind):
    return [(e["amount_cents"], e["title"]) for e in credits_of(headers)["entries"] if e["kind"] == kind]


# --- where credits come from ---


def test_admin_grants_credits(client, make_user, admin, credits_of):
    sun = make_user("sun", credits=0)
    assert credits_of(sun) == {"balance_cents": 0, "entries": [], "next_cursor": None}

    resp = client.post(
        "/admin/credits/grants",
        json={"email": sun.email, "amount_cents": 1500, "description": "Welcome gift"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["balance_cents"] == 1500
    entry = resp.json()["entry"]
    assert (entry["kind"], entry["amount_cents"], entry["title"]) == ("grant", 1500, "Welcome gift")

    ledger = credits_of(sun)
    assert ledger["balance_cents"] == 1500
    assert [e["id"] for e in ledger["entries"]] == [entry["id"]]
    assert set(ledger["entries"][0]) == {
        "id", "kind", "amount_cents", "balance_after_cents", "trip_id", "trip", "refund_of_id",
        "refund_of_payer", "refunded_cents", "refunded_to", "description", "created_at", "title",
    }
    # The admin can see the same thing.
    assert client.get(f"/admin/users/{sun.user_id}/credits", headers=admin).json() == ledger


def test_grant_validation(client, make_user, admin):
    sun = make_user("sun", credits=0)
    url = "/admin/credits/grants"
    assert client.post(url, json={"email": sun.email, "amount_cents": 100}, headers=sun).status_code == 403
    assert client.post(url, json={"email": sun.email, "amount_cents": 0}, headers=admin).status_code == 422
    assert client.post(url, json={"amount_cents": 100}, headers=admin).status_code == 422
    both = {"user_id": sun.user_id, "email": sun.email, "amount_cents": 100}
    assert client.post(url, json=both, headers=admin).status_code == 422
    nobody = client.post(url, json={"email": "nobody@example.com", "amount_cents": 100}, headers=admin)
    assert nobody.status_code == 422
    assert "nobody@example.com" in nobody.json()["detail"]

    receipt = {"user_id": sun.user_id, "amount_cents": 500, "external_id": "apple-txn-1"}
    assert client.post(url, json=receipt, headers=admin).status_code == 201
    again = client.post(url, json=receipt, headers=admin)
    assert again.status_code == 409
    assert client.get("/credits", headers=sun).json()["balance_cents"] == 500


def test_balance_is_not_on_the_user_profile(client, make_user):
    sun = make_user("sun")
    assert set(client.get("/users/me", headers=sun).json()) == {
        "id", "email", "display_name", "avatar_url", "created_at"
    }


def test_rate_tables_are_public(client):
    resp = client.get("/pricing/schedule")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["key"], body["included_members"]) == ("2026-11", 5)
    assert body["trip_brackets"][0] == {"first_day": 1, "last_day": 3, "cents_per_day": 160}
    assert body["trip_brackets"][-1] == {"first_day": 31, "last_day": None, "cents_per_day": 20}
    assert body["member_brackets"][0] == {"first_day": 1, "last_day": 10, "cents_per_day": 20}
    assert client.get("/pricing/schedule", params={"key": "1999-01"}).status_code == 404


# --- creating a trip ---


def test_quote_before_creating(client, make_user, credits_of):
    sun, broke = make_user("sun"), make_user("broke", credits=0)

    quote = client.post("/trips/quote", json=JAPAN_TRIP, headers=sun)
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert (body["days"], body["members"], body["extra_members"]) == (7, 1, 0)
    assert (body["owed_cents"], body["paid_cents"], body["price_cents"]) == (800, 0, 800)
    assert (body["balance_cents"], body["shortfall_cents"]) == (STARTING_CREDITS, 0)
    assert body["trip_lines"] == [
        {"first_day": 1, "last_day": 3, "days": 3, "cents_per_day": 160, "cents": 480},
        {"first_day": 4, "last_day": 7, "days": 4, "cents_per_day": 80, "cents": 320},
    ]
    # A quote makes nothing and spends nothing.
    assert client.get("/trips", headers=sun).json() == []
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS

    six = {**JAPAN_TRIP, "members": [{"email": f"p{i}@example.com"} for i in range(5)]}
    assert client.post("/trips/quote", json=six, headers=sun).json()["price_cents"] == 940
    short = client.post("/trips/quote", json=JAPAN_TRIP, headers=broke).json()
    assert (short["balance_cents"], short["shortfall_cents"]) == (0, 800)


def test_creating_charges_the_creator(client, make_user, make_trip, credits_of):
    sun = make_user("sun")
    trip = make_trip(sun)
    assert trip["price_cents"] == 800

    ledger = credits_of(sun)
    assert ledger["balance_cents"] == STARTING_CREDITS - 800
    charge = ledger["entries"][0]
    assert (charge["kind"], charge["amount_cents"], charge["trip_id"]) == ("trip_charge", -800, trip["id"])
    assert charge["title"] == "Unlocked Japan"

    assert client.get(f"/trips/{trip['id']}", headers=sun).json()["price_cents"] == 800
    assert client.get("/trips", headers=sun).json()[0]["price_cents"] == 800
    itinerary = client.get(f"/trips/{trip['id']}/itinerary", headers=sun).json()
    assert itinerary["trip"]["price_cents"] == 800


def test_too_few_credits_then_buy_then_the_same_request_succeeds(
    client, make_user, grant_credits, credits_of
):
    broke = make_user("broke", credits=300)
    resp = client.post("/trips", json=JAPAN_TRIP, headers=broke)
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"] == {
        "message": "Not enough credits",
        "price_cents": 800,
        "balance_cents": 300,
        "shortfall_cents": 500,
    }
    # Nothing was made and nothing was spent.
    assert client.get("/trips", headers=broke).json() == []
    assert credits_of(broke)["balance_cents"] == 300

    grant_credits(broke, 500)
    resp = client.post("/trips", json=JAPAN_TRIP, headers=broke)
    assert resp.status_code == 201, resp.text
    assert credits_of(broke)["balance_cents"] == 0


def test_a_stale_quote_is_refused(client, make_user, credits_of):
    sun = make_user("sun")
    resp = client.post("/trips", json={**JAPAN_TRIP, "expected_price_cents": 700}, headers=sun)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {
        "message": "Price changed since it was quoted",
        "expected_price_cents": 700,
        "price_cents": 800,
    }
    assert client.get("/trips", headers=sun).json() == []
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS

    resp = client.post("/trips", json={**JAPAN_TRIP, "expected_price_cents": 800}, headers=sun)
    assert resp.status_code == 201, resp.text


def test_an_extension_is_paid_for_by_whoever_extends(client, make_user, make_trip, credits_of):
    sun, amy = make_user("sun"), make_user("amy")
    japan = make_trip(sun, members=[{"email": amy.email}])
    early = client.post(
        "/trips",
        json={"title": "Early", "start_date": "2026-04-07", "end_date": "2026-04-09",
              "extends_trip_id": japan["id"]},
        headers=amy,
    )
    assert early.status_code == 201, early.text
    assert early.json()["price_cents"] == 480
    assert credits_of(amy)["balance_cents"] == STARTING_CREDITS - 480
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800


# --- editing a trip ---


def test_quote_for_an_edit(client, make_user, make_trip, clock):
    sun = make_user("sun")
    trip = make_trip(sun, **FUTURE_TRIP, members=[{"email": "amy@example.com"}])
    url = f"/trips/{trip['id']}/quote"

    nothing = client.post(url, json={"title": "Porto"}, headers=sun).json()
    assert (nothing["owed_cents"], nothing["paid_cents"], nothing["price_cents"], nothing["refund_cents"]) == (800, 800, 0, 0)

    longer = client.post(url, json={"end_date": "2026-12-09"}, headers=sun).json()
    assert (longer["days"], longer["members"], longer["price_cents"]) == (9, 2, 160)

    # `members` left out keeps the roster; listed, it replaces it (owner always counted).
    roster = [{"email": f"p{i}@example.com"} for i in range(5)]
    assert client.post(url, json={"members": roster}, headers=sun).json()["members"] == 6
    assert client.post(url, json={"members": roster}, headers=sun).json()["price_cents"] == 140
    assert client.post(url, json={"members": []}, headers=sun).json()["members"] == 1

    shorter = client.post(url, json={"end_date": "2026-12-04"}, headers=sun).json()
    assert (shorter["refund_window_open"], shorter["refund_cents"], shorter["price_cents"]) == (True, 240, 0)
    clock.now = CUTOFF
    late = client.post(url, json={"end_date": "2026-12-04"}, headers=sun).json()
    assert (late["refund_window_open"], late["refund_cents"]) == (False, 0)

    assert client.post(url, json={"end_date": "2026-11-30"}, headers=sun).status_code == 422
    assert client.post(url, json={"start_date": None}, headers=sun).status_code == 422


def test_a_paid_edit_charges_whoever_makes_it(client, make_user, make_trip, credits_of):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, members=[{"email": sean.email, "role": "organizer"}])
    url = f"/trips/{trip['id']}"

    resp = client.patch(url, json={"end_date": "2026-04-17", "expected_price_cents": 160}, headers=sean)
    assert resp.status_code == 200, resp.text
    assert resp.json()["price_cents"] == 960
    assert credits_of(sean)["balance_cents"] == STARTING_CREDITS - 160
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800
    assert _charges(credits_of, sean, "trip_charge") == [(-160, "Unlocked Japan")]

    # The wrong expectation changes nothing.
    stale = client.patch(url, json={"end_date": "2026-04-19", "expected_price_cents": 1}, headers=sean)
    assert stale.status_code == 409
    assert client.get(url, headers=sun).json()["end_date"] == "2026-04-17"


def test_a_refused_edit_leaves_the_trip_untouched(client, make_user, make_trip, credits_of):
    sun, broke = make_user("sun"), make_user("broke", credits=0)
    trip = make_trip(sun, members=[{"email": broke.email, "role": "organizer"}])
    url = f"/trips/{trip['id']}"

    resp = client.patch(url, json={"title": "Longer Japan", "end_date": "2026-04-17"}, headers=broke)
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["shortfall_cents"] == 160
    after = client.get(url, headers=sun).json()
    assert (after["title"], after["end_date"]) == ("Japan", "2026-04-15")
    assert len(client.get(f"{url}/days", headers=sun).json()) == 7
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800

    # Free edits by the broke organizer still go through.
    assert client.patch(url, json={"title": "Same Japan"}, headers=broke).status_code == 200


def test_shrinking_refunds_until_a_day_after_the_start(client, make_user, make_trip, credits_of, clock):
    sun = make_user("sun")
    trip = make_trip(sun, **FUTURE_TRIP)
    url = f"/trips/{trip['id']}"

    clock.now = CUTOFF.replace(hour=23, day=1)  # an hour before the cutoff
    assert client.patch(url, json={"end_date": "2026-12-06"}, headers=sun).status_code == 200
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 720
    refund = credits_of(sun)["entries"][0]
    assert (refund["kind"], refund["amount_cents"], refund["title"]) == ("trip_refund", 80, "Refund: Lisbon")

    clock.now = CUTOFF
    assert client.patch(url, json={"end_date": "2026-12-05"}, headers=sun).status_code == 200
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 720  # free, but nothing back
    assert client.get(url, headers=sun).json()["price_cents"] == 640

    # Growing it again only charges what's beyond what was paid.
    assert client.patch(url, json={"end_date": "2026-12-06"}, headers=sun).status_code == 200
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 720
    assert client.patch(url, json={"end_date": "2026-12-07"}, headers=sun).status_code == 200
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800


def test_refunds_always_go_to_the_owner(client, make_user, make_trip, credits_of, session):
    sun, sean = make_user("sun"), make_user("sean")
    trip = make_trip(sun, **FUTURE_TRIP, members=[{"email": sean.email, "role": "organizer"}])
    url = f"/trips/{trip['id']}"
    assert client.patch(url, json={"end_date": "2026-12-09"}, headers=sean).status_code == 200  # +160, sean

    # Down to 6 days: 720 owed, 960 paid. The 240 goes to Sun, who owns the
    # trip, not to Sean, who paid the last 160; they sort that out between
    # themselves for now.
    assert client.patch(url, json={"end_date": "2026-12-06"}, headers=sean).status_code == 200
    assert credits_of(sean)["balance_cents"] == STARTING_CREDITS - 160
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800 + 240
    assert _charges(credits_of, sean, "trip_refund") == []
    assert _charges(credits_of, sun, "trip_refund") == [(80, "Refund: Lisbon"), (160, "Refund: Lisbon")]

    # Cancelling gives the owner the rest, whoever paid it.
    assert client.delete(url, headers=sun).status_code == 204
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS + 160
    assert credits_of(sean)["balance_cents"] == STARTING_CREDITS - 160

    # Yet every refund row says whose charge it came out of, latest charge
    # first, so a fairer split can be worked out later.
    rows = session.scalars(
        select(CreditEntry).where(CreditEntry.trip_id == trip["id"]).order_by(CreditEntry.seq)
    ).all()
    by_id = {row.id: row for row in rows}
    assert [
        (str(r.user_id), r.kind.value, r.amount_cents, by_id[r.refund_of_id].amount_cents if r.refund_of_id else None)
        for r in rows
    ] == [
        (sun.user_id, "trip_charge", -800, None),
        (sean.user_id, "trip_charge", -160, None),
        (sun.user_id, "trip_refund", 160, -160),  # 240 back: all of Sean's charge first...
        (sun.user_id, "trip_refund", 80, -800),  # ...then 80 of Sun's
        (sun.user_id, "trip_refund", 720, -800),  # cancellation: the rest of Sun's
    ]


def test_deleting_refunds_inside_the_window(client, make_user, make_trip, credits_of, clock):
    sun = make_user("sun")
    trip = make_trip(sun, **FUTURE_TRIP)
    assert client.delete(f"/trips/{trip['id']}", headers=sun).status_code == 204
    ledger = credits_of(sun)
    assert ledger["balance_cents"] == STARTING_CREDITS
    # The rows outlive the trip, still keyed to it and saying what it was.
    assert [(e["kind"], e["amount_cents"], e["trip_id"], e["title"]) for e in ledger["entries"][:2]] == [
        ("trip_refund", 800, trip["id"], "Refund: Lisbon"),
        ("trip_charge", -800, trip["id"], "Unlocked Lisbon"),
    ]

    clock.now = CUTOFF
    later = make_trip(sun, **FUTURE_TRIP)
    assert client.delete(f"/trips/{later['id']}", headers=sun).status_code == 204
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800


# --- the roster ---


def test_people_beyond_five_cost_extra(client, make_user, make_trip, credits_of):
    sun, broke = make_user("sun"), make_user("broke", credits=0)
    four = [{"email": f"p{i}@example.com"} for i in range(3)] + [{"email": broke.email, "role": "organizer"}]
    trip = make_trip(sun, **FUTURE_TRIP, members=four)
    assert trip["price_cents"] == 800  # five people, owner included
    url = f"/trips/{trip['id']}/members"

    sixth = client.post(url, json={"email": "six@example.com"}, headers=sun)
    assert sixth.status_code == 201, sixth.text
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 940

    # The seventh is cheaper: extra member-days 8-14, partly at the 15c rate.
    seventh = client.post(url, json={"email": "seven@example.com"}, headers=broke)
    assert seventh.status_code == 402
    assert seventh.json()["detail"]["shortfall_cents"] == 120
    assert len(client.get(url, headers=sun).json()) == 6

    # Removing the sixth gives Sun's 140 back (the trip hasn't started).
    assert client.delete(f"{url}/{sixth.json()['id']}", headers=sun).status_code == 204
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800


def test_leaving_refunds_the_owner_not_the_leaver(client, make_user, make_trip, credits_of):
    sun, amy = make_user("sun"), make_user("amy")
    five = [{"email": f"p{i}@example.com"} for i in range(4)]
    trip = make_trip(sun, **FUTURE_TRIP, members=five)
    url = f"/trips/{trip['id']}/members"
    added = client.post(url, json={"email": amy.email}, headers=sun).json()
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 940

    assert client.delete(f"{url}/{added['id']}", headers=amy).status_code == 204
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800
    assert credits_of(amy)["balance_cents"] == STARTING_CREDITS


def test_a_trip_keeps_the_rates_it_was_made_under(client, make_user, make_trip, monkeypatch, credits_of):
    sun = make_user("sun")
    old = make_trip(sun)

    doubled = pricing.schedule("2026-11")
    monkeypatch.setitem(
        pricing.SCHEDULES,
        "test-2x",
        Schedule(
            key="test-2x",
            included_members=5,
            trip_brackets=tuple(Bracket(b.first_day, b.last_day, b.cents_per_day * 2) for b in doubled.trip_brackets),
            member_brackets=doubled.member_brackets,
        ),
    )
    monkeypatch.setattr(pricing, "CURRENT_SCHEDULE", "test-2x")

    new = make_trip(sun)
    assert new["price_cents"] == 1600
    # Each trip says which rate table it's on, so the app can price edits
    # with that table's brackets.
    assert (old["price_schedule"], new["price_schedule"]) == ("2026-11", "test-2x")
    assert client.get(f"/trips/{old['id']}", headers=sun).json()["price_schedule"] == "2026-11"
    assert {t["price_schedule"] for t in client.get("/trips", headers=sun).json()} == {"2026-11", "test-2x"}
    table = client.get("/pricing/schedule", params={"key": new["price_schedule"]}).json()
    assert table["trip_brackets"][0]["cents_per_day"] == 320
    assert client.post("/trips/quote", json=JAPAN_TRIP, headers=sun).json()["schedule"] == "test-2x"
    assert client.get(f"/trips/{old['id']}", headers=sun).json()["price_cents"] == 800
    quote = client.post(f"/trips/{old['id']}/quote", json={"end_date": "2026-04-16"}, headers=sun).json()
    assert (quote["schedule"], quote["price_cents"]) == ("2026-11", 80)
    assert client.patch(f"/trips/{old['id']}", json={"end_date": "2026-04-16"}, headers=sun).status_code == 200
    assert credits_of(sun)["balance_cents"] == STARTING_CREDITS - 800 - 1600 - 80


def test_extensions_carry_their_own_schedule(client, make_user, make_trip, monkeypatch):
    sun = make_user("sun")
    lead = make_trip(sun, **FUTURE_TRIP)
    monkeypatch.setitem(pricing.SCHEDULES, "test-new", pricing.schedule("2026-11"))
    monkeypatch.setattr(pricing, "CURRENT_SCHEDULE", "test-new")

    later = {"title": "Porto", "start_date": "2026-12-07", "end_date": "2026-12-09"}
    extension = make_trip(sun, **later, extends_trip_id=lead["id"])
    assert extension["price_schedule"] == "test-new"
    [entry] = client.get("/trips", headers=sun).json()
    assert entry["price_schedule"] == "2026-11"
    assert [e["price_schedule"] for e in entry["extensions"]] == ["test-new"]
