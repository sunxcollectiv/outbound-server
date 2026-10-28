"""Apple Push Notification service, with token (.p8 key) auth over HTTP/2.

Each request carries a short-lived JWT signed with the key (ES256). Apple
rejects one older than an hour and also refuses one renewed more often than
every 20 minutes, so a single token is reused and renewed after 50 minutes.
See https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import jwt

from app.config import Settings, get_settings

SANDBOX_HOST = "https://api.sandbox.push.apple.com"
PRODUCTION_HOST = "https://api.push.apple.com"

# Renew the provider token after this long (Apple's limit is 60 minutes).
TOKEN_LIFETIME_SECONDS = 50 * 60

# Apple caps apns-collapse-id at 64 bytes.
_COLLAPSE_ID_MAX_BYTES = 64


@dataclass(frozen=True)
class ApnsResponse:
    status: int
    # Apple's reason code on failure, e.g. "BadDeviceToken" or "Unregistered".
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def token_is_dead(self) -> bool:
        """The device token will never work again: forget it. (BadDeviceToken
        also comes back for a sandbox token sent to production or vice versa, so
        a wrong apns_use_sandbox prunes good tokens.)"""
        return self.status == 410 or (self.status == 400 and self.reason == "BadDeviceToken")


def collapse_id(key: str) -> str:
    """An apns-collapse-id for `key`: the key itself when short enough, else a
    digest of it, so a long dedupe key still collapses consistently."""
    if len(key.encode()) <= _COLLAPSE_ID_MAX_BYTES:
        return key
    return hashlib.sha256(key.encode()).hexdigest()


class ApnsClient:
    """Sends alert pushes to one app (`topic`, its bundle id). Thread-safe:
    background senders share it, and the provider token is renewed under a
    lock."""

    def __init__(
        self,
        *,
        key_id: str,
        team_id: str,
        private_key: str,
        topic: str,
        sandbox: bool = True,
        http: httpx.Client | None = None,
    ) -> None:
        self.topic = topic
        self._key_id = key_id
        self._team_id = team_id
        self._private_key = private_key
        self._http = http or httpx.Client(
            base_url=SANDBOX_HOST if sandbox else PRODUCTION_HOST, http2=True, timeout=10
        )
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_issued_at = 0.0

    def _provider_token(self, *, renew: bool = False) -> str:
        with self._lock:
            now = time.time()
            if renew or self._token is None or now - self._token_issued_at > TOKEN_LIFETIME_SECONDS:
                self._token = jwt.encode(
                    {"iss": self._team_id, "iat": int(now)},
                    self._private_key,
                    algorithm="ES256",
                    headers={"kid": self._key_id},
                )
                self._token_issued_at = now
            return self._token

    def send(
        self, device_token: str, payload: dict[str, Any], *, collapse_key: str | None = None
    ) -> ApnsResponse:
        """Send one alert push. Raises httpx.HTTPError if Apple can't be
        reached; any answer from Apple, good or bad, comes back as a response."""
        headers = {
            "apns-topic": self.topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if collapse_key is not None:
            headers["apns-collapse-id"] = collapse_id(collapse_key)
        body = json.dumps(payload, separators=(",", ":"))

        response = self._post(device_token, body, headers, self._provider_token())
        if response.reason == "ExpiredProviderToken":
            response = self._post(device_token, body, headers, self._provider_token(renew=True))
        return response

    def _post(
        self, device_token: str, body: str, headers: dict[str, str], provider_token: str
    ) -> ApnsResponse:
        response = self._http.post(
            f"/3/device/{device_token}",
            content=body,
            headers={**headers, "authorization": f"bearer {provider_token}"},
        )
        if response.status_code == 200:
            return ApnsResponse(200)
        try:
            reason = response.json().get("reason")
        except ValueError:
            reason = None
        return ApnsResponse(response.status_code, reason)


def build_apns_client(settings: Settings) -> ApnsClient | None:
    """The client `settings` describe, or None when push isn't set up (no key,
    key id or team id)."""
    private_key = settings.apns_private_key
    if not private_key and settings.apns_key_path:
        private_key = Path(settings.apns_key_path).expanduser().read_text()
    if not (private_key and settings.apns_key_id and settings.apns_team_id):
        return None
    return ApnsClient(
        key_id=settings.apns_key_id,
        team_id=settings.apns_team_id,
        # A key pasted into .env on one line keeps its newlines as "\n".
        private_key=private_key.replace("\\n", "\n"),
        topic=settings.apns_topic,
        sandbox=settings.apns_use_sandbox,
    )


@lru_cache
def get_apns_client() -> ApnsClient | None:
    """One client for the process, so its HTTP/2 connection and provider token
    are reused across pushes."""
    return build_apns_client(get_settings())
