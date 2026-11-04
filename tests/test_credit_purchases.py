"""Buying credits in the app: POST /credits/purchases with a StoreKit 2
signed transaction. Transactions here come from the Xcode environment (the
simulator against Outbound.storekit), which Xcode signs itself, so they can
be built in a test; anything claiming to be from Apple must verify."""


import jwt
import pytest

from app.main import app
from app.services.storekit import StoreKitVerifier, get_storekit_verifier

URL = "/credits/purchases"
PURCHASED_AT = 1_762_300_000_000  # ms


def _transaction(environment="Xcode", **fields):
    payload = {
        "transactionId": "0",
        "originalTransactionId": "0",
        "bundleId": "earth.outbound",
        "productId": "earth.outbound.credits.1500",
        "purchaseDate": PURCHASED_AT,
        "originalPurchaseDate": PURCHASED_AT,
        "quantity": 1,
        "type": "Consumable",
        "inAppOwnershipType": "PURCHASED",
        "signedDate": PURCHASED_AT,
        "environment": environment,
        **fields,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, "xcode signs these itself, not apple", algorithm="HS256")


@pytest.fixture
def storekit(client):
    """The verifier a local server runs with: Xcode purchases allowed."""
    verifier = StoreKitVerifier.for_environments(bundle_id="earth.outbound", allow_xcode=True)
    app.dependency_overrides[get_storekit_verifier] = lambda: verifier
    return verifier


def _buy(client, headers, signed):
    return client.post(URL, json={"signed_transaction": signed}, headers=headers)


def test_a_purchase_is_credited_once(client, make_user, storekit, credits_of):
    sun = make_user("sun", credits=0)
    signed = _transaction(appAccountToken=sun.user_id)

    first = _buy(client, sun, signed)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["balance_cents"] == 1500
    entry = body["entries"][0]
    assert (entry["kind"], entry["amount_cents"], entry["title"], entry["balance_after_cents"]) == (
        "purchase", 1500, "Credits purchased", 1500,
    )

    # The app resends until it hears back; a repeat changes nothing.
    again = _buy(client, sun, signed)
    assert again.status_code == 200
    assert again.json() == body
    assert credits_of(sun)["balance_cents"] == 1500


def test_quantity_multiplies(client, make_user, storekit):
    sun = make_user("sun", credits=0)
    resp = _buy(client, sun, _transaction(productId="earth.outbound.credits.500", quantity=3))
    assert resp.status_code == 201
    assert resp.json()["balance_cents"] == 1500


def test_a_purchase_belongs_to_one_account(client, make_user, storekit, credits_of):
    sun, amy = make_user("sun", credits=0), make_user("amy", credits=0)
    # Bought for Sun: nobody else can send it in.
    for_sun = _transaction(appAccountToken=sun.user_id)
    assert _buy(client, amy, for_sun).status_code == 403

    # Bought without an account tag: whoever sends it first gets it.
    untagged = _transaction(transactionId="7")
    assert _buy(client, sun, untagged).status_code == 201
    taken = _buy(client, amy, untagged)
    assert taken.status_code == 409
    assert credits_of(amy)["balance_cents"] == 0
    assert credits_of(sun)["balance_cents"] == 1500


def test_xcode_transaction_ids_restart(client, make_user, storekit, credits_of):
    """Clearing Xcode's test transactions starts the ids from 0 again; a new
    purchase with an old id is still a new purchase."""
    sun = make_user("sun", credits=0)
    assert _buy(client, sun, _transaction()).status_code == 201
    later = _transaction(purchaseDate=PURCHASED_AT + 60_000)
    assert _buy(client, sun, later).status_code == 201
    assert credits_of(sun)["balance_cents"] == 3000


@pytest.mark.parametrize(
    ("signed", "detail"),
    [
        (_transaction(productId="earth.outbound.premium"), "Not a credits product"),
        (_transaction(type="Non-Consumable"), "Not a credits product"),
        (_transaction(revocationDate=PURCHASED_AT + 1), "refunded or revoked"),
        (_transaction(bundleId="com.someone.else"), "couldn't verify"),
        (_transaction(appAccountToken="not-a-uuid"), "unknown account"),
        (_transaction(transactionId=None), "couldn't verify"),
        ("not a jws", "isn't a signed App Store transaction"),
        # Claims to be from Apple, but isn't signed by Apple.
        (_transaction(environment="Sandbox"), "couldn't verify"),
        # Production isn't enabled until the app's App Store id is configured.
        (_transaction(environment="Production"), "Production environment aren't accepted"),
    ],
)
def test_refused_purchases_credit_nothing(client, make_user, storekit, credits_of, signed, detail):
    sun = make_user("sun", credits=0)
    resp = _buy(client, sun, signed)
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]
    assert credits_of(sun) == {"balance_cents": 0, "entries": [], "next_cursor": None}


def test_xcode_purchases_are_refused_unless_allowed(client, make_user):
    """By default (any deployed server) a simulator purchase is worthless:
    nothing about it can be verified."""
    sun = make_user("sun", credits=0)
    app.dependency_overrides[get_storekit_verifier] = lambda: StoreKitVerifier.for_environments(
        bundle_id="earth.outbound"
    )
    resp = _buy(client, sun, _transaction())
    assert resp.status_code == 422
    assert "Xcode environment aren't accepted" in resp.json()["detail"]


def test_needs_a_signed_in_user(client):
    assert client.post(URL, json={"signed_transaction": _transaction()}).status_code == 401

