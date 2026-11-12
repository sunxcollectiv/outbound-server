"""Calls from outside services. No bearer token: each route checks the
caller's signature instead."""
import json
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import Now, SessionDep
from app.config import get_settings
from app.db import SessionLocal
from app.services import resend_webhook
from app.services.mail_extraction import MailExtractor, get_mail_extractor
from app.services.mail_ingest import SessionFactory, process_email_in_own_session
from app.services.received_mail import store_received_webhook
from app.services.resend import ResendClient, get_resend_client

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

EMAIL_RECEIVED = "email.received"


def get_resend_webhook_secret() -> str | None:
    return get_settings().resend_webhook_secret


def get_session_factory() -> SessionFactory:
    """Where work that outlives the request gets its session (the request's
    own is closed by then)."""
    return SessionLocal


async def get_raw_body(request: Request) -> bytes:
    """The request bytes exactly as sent: the signature covers these, not a
    re-serialized parse of them."""
    return await request.body()


class WebhookAck(BaseModel):
    # `accepted`: stored and queued for processing. `ignored`: an event we
    # don't act on, acknowledged so Resend doesn't keep retrying it.
    status: Literal["accepted", "ignored"]


@router.post("/resend", response_model=WebhookAck, summary="Resend webhook")
def resend_webhook_event(
    request: Request,
    body: Annotated[bytes, Depends(get_raw_body)],
    background: BackgroundTasks,
    session: SessionDep,
    now: Now,
    secret: Annotated[str | None, Depends(get_resend_webhook_secret)],
    resend: Annotated[ResendClient, Depends(get_resend_client)],
    extractor: Annotated[MailExtractor, Depends(get_mail_extractor)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> WebhookAck:
    """Where Resend posts its webhook events. An `email.received` event is
    stored as a receipt straight away, then, once this has answered, run through
    the same pipeline as `POST /admin/received/mail/process`: sender check,
    extraction, and import into the sender's matching trip. Any other event
    type is acknowledged and ignored.

    The call must carry Resend's (Svix) signature for `RESEND_WEBHOOK_SECRET`:
    401 without a valid one, 503 while no secret is configured. Resend retries
    anything but a 2xx, and a redelivered email is not imported twice."""
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The Resend webhook is not configured"
        )
    try:
        resend_webhook.verify(secret, request.headers, body, now=now)
    except resend_webhook.WebhookSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from None
    if not isinstance(event, dict) or event.get("type") != EMAIL_RECEIVED:
        return WebhookAck(status="ignored")
    data = event.get("data")
    receipt = store_received_webhook(session, data) if isinstance(data, dict) else None
    if receipt is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Event names no email")
    session.commit()

    background.add_task(
        process_email_in_own_session,
        session_factory,
        receipt.resend_email_id,
        resend=resend,
        extractor=extractor,
    )
    return WebhookAck(status="accepted")
