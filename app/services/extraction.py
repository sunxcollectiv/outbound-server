"""The source-neutral core of booking extraction: the structured result schema,
the prompt block that describes it, and the JSON parsing — shared by the mail and
image extraction services.

What differs per source is ingestion (an email body vs. image bytes) and the
framing sentence at the top of the prompt. What does NOT differ is the shape we
ask Claude for and how we validate the reply, so those live here in one place:
the schema must never drift between sources, since both feed the same later
trip-item step.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, ValidationError


class ExtractionError(Exception):
    """Base for a failed extraction (bad/unparseable model reply, or an upstream
    model error). The per-source services subclass this."""


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


# The schema + rules half of the system prompt, shared verbatim by every source.
# Each service prepends its own one-paragraph framing (what the input is) before
# this block. We ask for JSON in the prompt and validate it with the models above
# rather than the API's strict structured outputs — our nested schema (items →
# places + multi-leg segments) trips the structured-output complexity limit.
EXTRACTION_SCHEMA_PROMPT = """\
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
- category "not_a_booking" if this isn't a travel confirmation (marketing, an ad \
or newsletter, a receipt with no booking, or unrelated content).
- confidence: be conservative — a vague or ambiguous booking scores low.
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
guessing. Use null for any field the source doesn't state."""


def parse_extraction(response: anthropic.types.Message) -> Extraction:
    """Pull the single JSON object out of a Claude reply and validate it against
    `Extraction`. Tolerates stray prose around the object by slicing to the outer
    braces. Raises `ExtractionError` if there's no object or it doesn't validate."""
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ExtractionError("the model returned no JSON object")
    try:
        return Extraction.model_validate(json.loads(text[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ExtractionError(f"could not parse extraction: {exc}") from exc
