"""Classify + extract a travel-confirmation email in a single Claude call.

One Haiku pass does both jobs: it decides what kind of booking the email is (or
that it isn't one) and pulls out the structured details, returning a validated
`Extraction`. Cost optimization is deliberately deferred — the only concession is
that callers send the plaintext body, not the base64-inlined HTML.

The result schema and its JSON parsing are the source-neutral core in
`app.services.extraction` (shared with image extraction); this module adds the
email-specific framing and the Claude text call. The schema is shaped to map
cleanly onto `TripItem` later (kind, naive local wall-time + IANA zone per end,
place name/address, transit carrier/number and multi-leg segments), but this step
only extracts and stores — turning an `Extraction` into trip items is a separate,
later step.
"""
import anthropic

from app.config import get_settings
from app.services.extraction import (  # noqa: F401 — re-exported for callers/tests
    EXTRACTION_SCHEMA_PROMPT,
    ExtractedItem,
    ExtractedPlace,
    ExtractedSegment,
    Extraction,
    ExtractionError,
    parse_extraction,
    reference_date_line,
)

EXTRACTION_MODEL = "claude-haiku-5-5"

# The email-specific framing; the shared schema/rules block follows it.
_INTRO = """\
You extract structured travel-booking data from a single forwarded email.

The email was forwarded by a traveler to a trip inbox, so the real confirmation \
usually sits below forwarding headers and quoted text — read past them. Extract \
only what the email actually states; never invent details."""
_SYSTEM_PROMPT = f"{_INTRO}\n\n{EXTRACTION_SCHEMA_PROMPT}"


class MailExtractionError(ExtractionError):
    pass


class MailExtractionNotConfigured(MailExtractionError):
    pass


class MailExtractor:
    """Wraps the Claude call. Mirrors the other service clients: a key from
    settings, a NotConfigured error when it's unset, an injectable SDK client for
    tests."""

    def __init__(
        self,
        api_key: str | None,
        client: anthropic.Anthropic | None = None,
        model: str = EXTRACTION_MODEL,
        workspace_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._model = model
        self._workspace_id = workspace_id

    def _build_client(self) -> anthropic.Anthropic:
        # Org-level keys require naming the workspace on each request.
        headers = {"anthropic-workspace-id": self._workspace_id} if self._workspace_id else None
        return anthropic.Anthropic(api_key=self._api_key, default_headers=headers)

    def extract(self, *, subject: str | None, text_body: str | None) -> Extraction:
        if self._client is None and not self._api_key:
            raise MailExtractionNotConfigured("ANTHROPIC_API_KEY is not set")
        client = self._client or self._build_client()
        content = (
            f"{reference_date_line()}\n\n"
            f"Subject: {subject or '(no subject)'}\n\n{text_body or '(no body)'}"
        )
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=8000,
                system=[
                    {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.AnthropicError as exc:
            raise MailExtractionError(f"Claude extraction failed: {exc}") from exc
        try:
            return parse_extraction(response)
        except ExtractionError as exc:
            raise MailExtractionError(str(exc)) from exc


def get_mail_extractor() -> MailExtractor:
    settings = get_settings()
    return MailExtractor(
        settings.anthropic_api_key, workspace_id=settings.anthropic_workspace_id
    )
