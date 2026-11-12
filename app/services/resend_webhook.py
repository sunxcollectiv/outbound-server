"""Checking that a webhook call really came from Resend.

Resend delivers webhooks through Svix, which signs each one: the `svix-id`,
`svix-timestamp` (unix seconds) and `svix-signature` headers, where the
signature is base64(HMAC-SHA256(key, "{id}.{timestamp}.{raw body}")) and the
key is the webhook's signing secret with its `whsec_` prefix dropped and the
rest base64-decoded. The header can carry several space-separated
`v1,<signature>` entries (during a secret rotation); any one matching is enough.
See https://docs.svix.com/receiving/verifying-payloads/how-manual.

Done by hand rather than with the svix package: it's a dozen lines of stdlib.
"""
import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timedelta

SECRET_PREFIX = "whsec_"
# A call whose timestamp is further than this from now is refused, so a
# captured request can't be replayed later.
TIMESTAMP_TOLERANCE = timedelta(minutes=5)


class WebhookSignatureError(Exception):
    pass


def sign(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    """The base64 signature Svix would send for this message."""
    try:
        key = base64.b64decode(secret.removeprefix(SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WebhookSignatureError("The webhook secret is not valid base64") from exc
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def verify(secret: str, headers: Mapping[str, str], body: bytes, *, now: datetime) -> None:
    """Raise WebhookSignatureError unless `body` (the raw request bytes, not
    re-serialized JSON) carries a valid, recent signature for `secret`."""
    message_id = headers.get("svix-id")
    timestamp = headers.get("svix-timestamp")
    signatures = headers.get("svix-signature")
    if not (message_id and timestamp and signatures):
        raise WebhookSignatureError("Missing signature headers")

    try:
        sent_at = int(timestamp)
    except ValueError:
        raise WebhookSignatureError("Malformed timestamp") from None
    if abs(now.timestamp() - sent_at) > TIMESTAMP_TOLERANCE.total_seconds():
        raise WebhookSignatureError("Timestamp outside the allowed window")

    expected = sign(secret, message_id, timestamp, body).encode()
    for entry in signatures.split():
        version, _, signature = entry.partition(",")
        if version == "v1" and hmac.compare_digest(signature.encode(), expected):
            return
    raise WebhookSignatureError("No matching signature")
