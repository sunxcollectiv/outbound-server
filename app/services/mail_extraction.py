"""Classify + extract a travel-confirmation email in a single Claude call.

One Haiku pass does both jobs: it decides what kind of booking the email is (or
that it isn't one) and pulls out the structured details, returning a validated
`Extraction`. Cost optimization is deliberately deferred — the only concession is
that callers send the plaintext body, not the base64-inlined HTML.

The schema is shaped to map cleanly onto `TripItem` later (kind, naive local
wall-time + IANA zone per end, place name/address, transit carrier/number and
multi-leg segments), but this step only extracts and stores — turning an
`Extraction` into trip items is a separate, later step.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import get_settings

EXTRACTION_MODEL = "claude-haiku-5-5"

# We ask for JSON in the prompt and validate it with the pydantic models below,
# rather than the API's strict structured outputs — our nested schema (items →
# places + multi-leg segments) trips the structured-output complexity limit.
_SYSTEM_PROMPT = """\
You extract structured travel-booking data from a single forwarded email.

The email was forwarded by a traveler to a trip inbox, so the real confirmation \
usually sits below forwarding headers and quoted text — read past them. Extract \
only what the email actually states; never invent details.

Reply with ONE JSON object and nothing else — no prose, no markdown fences — in \
exactly this shape:

{
  "category": one of "flight" | "lodging" | "train" | "car_rental" | "restaurant" | "event" | "other" | "not_a_booking",
  "confidence": a number 0.0-1.0,
  "summary": "one short line describing the booking",
  "items": [
    {
      "kind": "lodging" | "activity" | "transit",
      "transit_mode": "flight" | "train" | "bus" | "ferry" | "car_rental" | "other" (transit only, else null),
      "title": "short title",
      "confirmation_number": string or null,
      "start_local": "YYYY-MM-DDTHH:MM" or null,
      "start_timezone": "IANA zone like Asia/Tokyo" or null,
      "end_local": "YYYY-MM-DDTHH:MM" or null,
      "end_timezone": "IANA zone" or null,
      "place": {"name": str|null, "address": str|null, "city": str|null, "iata_code": str|null} or null,
      "end_place": same shape or null,
      "segments": [
        {"transit_mode": "flight"|"train"|"bus"|"ferry"|"car_rental"|"other"|null,
         "carrier": str|null, "flight_number": str|null, "confirmation_number": str|null,
         "start_local": str|null, "start_timezone": str|null,
         "end_local": str|null, "end_timezone": str|null, "origin": place|null, "destination": place|null}
      ]
    }
  ]
}

Rules:
- category "not_a_booking" if this isn't a travel confirmation (marketing, a \
newsletter, a receipt with no booking, a reply, etc.).
- confidence: be conservative — a vague or ambiguous email scores low.
- kind "lodging" for stays (start = check-in, end = check-out), "transit" for \
flights/trains/ferries/buses/car rentals (start = departure, end = arrival; one \
entry per leg in "segments" with its mode, carrier and number), "activity" for \
anything else. Use "segments": [] for non-transit items.
- transit_mode: set each leg's mode, and the item's overall mode (the shared \
value when every leg is the same). Most bookings are one mode (a flight's legs \
are all flights); only a genuinely mixed booking (e.g. ferry + connecting bus) \
has differing leg modes. A leg's confirmation_number is only for a leg ticketed \
separately from the booking-level one; usually null.
- Times are naive local wall-clock with NO offset. Give the IANA timezone per \
end when you can tell it from the airport/city; null if unsure rather than \
guessing. Use null for any field the email doesn't state."""


class MailExtractionError(Exception):
    pass


class MailExtractionNotConfigured(MailExtractionError):
    pass


class ExtractedPlace(BaseModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    iata_code: str | None = None  # airport/station code, when applicable


class ExtractedSegment(BaseModel):
    """One leg of a multi-leg transit booking. Field names mirror
    `TripItemTransitSegment` so the later trip-item step maps 1:1."""

    transit_mode: str | None = None  # flight|train|bus|ferry|car_rental|other
    carrier: str | None = None
    flight_number: str | None = None  # flight/train number, e.g. "NH7"
    confirmation_number: str | None = None  # per-leg reference, if separate
    start_local: str | None = None
    start_timezone: str | None = None
    end_local: str | None = None
    end_timezone: str | None = None
    origin: ExtractedPlace | None = None
    destination: ExtractedPlace | None = None


class ExtractedItem(BaseModel):
    kind: Literal["lodging", "activity", "transit"]
    # Overall transport mode for a transit item (flight|train|bus|ferry|
    # car_rental|other). Null for non-transit.
    transit_mode: str | None = None
    title: str
    confirmation_number: str | None = None
    start_local: str | None = None
    start_timezone: str | None = None
    end_local: str | None = None
    end_timezone: str | None = None
    place: ExtractedPlace | None = None
    end_place: ExtractedPlace | None = None  # transit arrival
    segments: list[ExtractedSegment] = []  # transit legs; empty for other kinds


class Extraction(BaseModel):
    category: Literal[
        "flight", "lodging", "train", "car_rental", "restaurant", "event", "other", "not_a_booking"
    ]
    confidence: float
    summary: str
    items: list[ExtractedItem] = []


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
        content = f"Subject: {subject or '(no subject)'}\n\n{text_body or '(no body)'}"
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
        return self._parse(response)

    @staticmethod
    def _parse(response: anthropic.types.Message) -> Extraction:
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise MailExtractionError("Claude returned no JSON object")
        try:
            return Extraction.model_validate(json.loads(text[start : end + 1]))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise MailExtractionError(f"could not parse extraction: {exc}") from exc


def get_mail_extractor() -> MailExtractor:
    settings = get_settings()
    return MailExtractor(
        settings.anthropic_api_key, workspace_id=settings.anthropic_workspace_id
    )
