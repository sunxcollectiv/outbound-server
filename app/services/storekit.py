"""Verifying App Store purchases of credits.

The app buys a consumable through StoreKit 2 and sends us the transaction as
StoreKit signed it (a JWS). Apple's library checks the signature up to
Apple's root certificate, and that the transaction is for our app. Which App
Store environment a transaction comes from is read from the transaction, and
then held to: it's checked by a verifier for that environment only, and an
environment that isn't enabled (see Settings.storekit_*) is refused.

Xcode transactions (the simulator against Outbound.storekit) are signed by
Xcode, not Apple, so nothing about them can be verified; they're only for a
local server.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import jwt
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.Type import Type
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

from app.config import get_settings

ROOT_CERTIFICATE = Path(__file__).parent.parent / "certs" / "AppleRootCA-G3.cer"

# The credits each product buys, in cents: one consumable per amount on
# offer. Must match App Store Connect and the app's Outbound.storekit.
PRODUCTS: dict[str, int] = {
    "earth.outbound.credits.500": 500,
    "earth.outbound.credits.1500": 1_500,
    "earth.outbound.credits.2500": 2_500,
    "earth.outbound.credits.5000": 5_000,
}


class PurchaseNotVerified(Exception):
    pass


@dataclass(frozen=True)
class VerifiedPurchase:
    environment: Environment
    transaction_id: str
    product_id: str
    amount_cents: int
    # Who the app says it bought for (the account id it passed StoreKit).
    app_account_token: uuid.UUID | None
    purchase_date_ms: int

    @property
    def external_id(self) -> str:
        """The ledger's key for this purchase, so it's only credited once.
        Xcode numbers its transactions from 0 again whenever its test
        transactions are cleared, so those are keyed by time of purchase too."""
        key = f"apple:{self.environment.value.lower()}:{self.transaction_id}"
        if self.environment == Environment.XCODE:
            key += f":{self.purchase_date_ms}"
        return key


class StoreKitVerifier:
    def __init__(self, verifiers: dict[str, SignedDataVerifier]) -> None:
        # By environment name, as transactions give it ("Sandbox", ...).
        self._verifiers = verifiers

    @classmethod
    def for_environments(
        cls,
        *,
        bundle_id: str,
        app_apple_id: int | None = None,
        allow_sandbox: bool = True,
        allow_xcode: bool = False,
    ) -> StoreKitVerifier:
        roots = [ROOT_CERTIFICATE.read_bytes()]
        enabled = (
            (Environment.PRODUCTION, app_apple_id is not None),
            (Environment.SANDBOX, allow_sandbox),
            (Environment.XCODE, allow_xcode),
        )
        # Online checks would ask Apple whether its certificates were revoked:
        # a network round trip on every purchase, so off.
        return cls(
            {
                env.value: SignedDataVerifier(
                    roots,
                    enable_online_checks=False,
                    environment=env,
                    bundle_id=bundle_id,
                    app_apple_id=app_apple_id,
                )
                for env, on in enabled
                if on
            }
        )

    def verify(self, signed_transaction: str) -> VerifiedPurchase:
        """The purchase in a signed transaction, if it's genuine, for us, a
        credits product, and not refunded. Raises PurchaseNotVerified."""
        try:
            claimed = jwt.decode(signed_transaction, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise PurchaseNotVerified("That isn't a signed App Store transaction") from None
        verifier = self._verifiers.get(claimed.get("environment"))
        if verifier is None:
            raise PurchaseNotVerified(
                f"Purchases from the {claimed.get('environment')} environment aren't accepted here"
            )
        try:
            transaction = verifier.verify_and_decode_signed_transaction(signed_transaction)
        except Exception:  # noqa: BLE001 -- VerificationException, or a payload that won't parse
            raise PurchaseNotVerified("The App Store couldn't verify this purchase") from None

        amount = PRODUCTS.get(transaction.productId or "")
        if amount is None or transaction.type != Type.CONSUMABLE:
            raise PurchaseNotVerified(f"Not a credits product: {transaction.productId}")
        if transaction.revocationDate is not None:
            raise PurchaseNotVerified("That purchase was refunded or revoked")
        if transaction.transactionId is None or transaction.purchaseDate is None:
            raise PurchaseNotVerified("The App Store couldn't verify this purchase")
        token = None
        if transaction.appAccountToken:
            try:
                token = uuid.UUID(transaction.appAccountToken)
            except ValueError:
                raise PurchaseNotVerified("The purchase names an unknown account") from None
        return VerifiedPurchase(
            environment=transaction.environment,
            transaction_id=transaction.transactionId,
            product_id=transaction.productId,
            amount_cents=amount * (transaction.quantity or 1),
            app_account_token=token,
            purchase_date_ms=transaction.purchaseDate,
        )


@lru_cache
def get_storekit_verifier() -> StoreKitVerifier:
    settings = get_settings()
    return StoreKitVerifier.for_environments(
        bundle_id=settings.storekit_bundle_id,
        app_apple_id=settings.storekit_app_apple_id,
        allow_sandbox=settings.storekit_allow_sandbox,
        allow_xcode=settings.storekit_allow_xcode,
    )
