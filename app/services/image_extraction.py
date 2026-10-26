"""Classify + extract a travel booking from a single uploaded image in one Claude
vision call.

The image counterpart of `app.services.mail_extraction`: same one-pass classify +
extract, same validated `Extraction` result, same shared schema/parsing from
`app.services.extraction`. What differs is the input — the image bytes are sent
inline (base64) as a vision block instead of a plaintext body — and the framing.
Like mail, this step only extracts and stores; the trip-item step comes later.
"""
import base64

import anthropic

from app.config import get_settings
from app.services.extraction import (
    EXTRACTION_SCHEMA_PROMPT,
    Extraction,
    ExtractionError,
    parse_extraction,
    reference_date_line,
)

EXTRACTION_MODEL = "claude-haiku-5-5"

# The media types Claude's vision input accepts. An upload outside this set (e.g.
# image/heic from a phone) is failed fast with a clear reason rather than sent and
# rejected by the API; conversion is a later concern.
SUPPORTED_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

# The image-specific framing; the shared schema/rules block follows it.
_INTRO = """\
You extract structured travel-booking data from a single image — a screenshot or \
photo of a booking, such as a confirmation screen, a ticket, an itinerary, or a \
reservation. Read all the visible text, including small print. Extract only what \
the image actually shows; never invent details."""
_SYSTEM_PROMPT = f"{_INTRO}\n\n{EXTRACTION_SCHEMA_PROMPT}"


class ImageExtractionError(ExtractionError):
    pass


class ImageExtractionNotConfigured(ImageExtractionError):
    pass


class ImageExtractor:
    """Wraps the Claude vision call. Mirrors `MailExtractor`: a key from settings,
    a NotConfigured error when it's unset, an injectable SDK client for tests."""

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

    def extract(self, *, data: bytes, content_type: str) -> Extraction:
        if self._client is None and not self._api_key:
            raise ImageExtractionNotConfigured("ANTHROPIC_API_KEY is not set")
        if content_type not in SUPPORTED_MEDIA_TYPES:
            raise ImageExtractionError(f"unsupported image type for extraction: {content_type}")
        client = self._client or self._build_client()
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": content_type,
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            },
            {
                "type": "text",
                "text": f"{reference_date_line()}\n\nExtract the travel booking shown in this image.",
            },
        ]
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
            raise ImageExtractionError(f"Claude extraction failed: {exc}") from exc
        try:
            return parse_extraction(response)
        except ExtractionError as exc:
            raise ImageExtractionError(str(exc)) from exc


def get_image_extractor() -> ImageExtractor:
    settings = get_settings()
    return ImageExtractor(
        settings.anthropic_api_key, workspace_id=settings.anthropic_workspace_id
    )
