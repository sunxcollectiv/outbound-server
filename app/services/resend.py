from typing import Any

import httpx

from app.config import get_settings

# Resend's REST API. We only talk to the inbound ("receiving") side here —
# sending lives elsewhere. The received-email endpoints are in private beta and
# may change before GA. See https://resend.com/docs/api-reference/emails.
RESEND_BASE_URL = "https://api.resend.com"
RECEIVING_PATH = "/emails/receiving"


class ResendError(Exception):
    pass


class ResendNotConfigured(ResendError):
    pass


class ResendClient:
    """Thin httpx wrapper over the Resend inbound email API. Modelled on
    GooglePlacesClient: a bare key plus an injectable http client, and plain
    dicts out so the schema layer decides what to surface."""

    def __init__(self, api_key: str | None, http: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(base_url=RESEND_BASE_URL, timeout=10)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._api_key:
            raise ResendNotConfigured("RESEND_API_KEY is not set")
        try:
            response = self._http.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as exc:
            raise ResendError(f"Resend request failed: {exc}") from exc
        if response.status_code != 200:
            raise ResendError(f"Resend returned {response.status_code}: {response.text}")
        return response.json()

    def list_received_emails(
        self,
        *,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Resend's received-email list response, returned as-is. Newest first;
        metadata only (no body). `limit` (1-100) bounds the page; `before`/`after`
        are email-id cursors for paging further back (Resend has no time filter)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return self._get(RECEIVING_PATH, params or None)

    def get_received_email(self, email_id: str, *, html_format: str | None = None) -> dict[str, Any]:
        """One received email with its body (`text`/`html`), headers, and
        attachment metadata — the list endpoint omits all of that. Processing
        fetches this per email to feed the extractor."""
        params = {"html_format": html_format} if html_format else None
        return self._get(f"{RECEIVING_PATH}/{email_id}", params)


def get_resend_client() -> ResendClient:
    return ResendClient(get_settings().resend_api_key)
