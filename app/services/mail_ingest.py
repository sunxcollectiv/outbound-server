"""Running one stored email all the way through the pipeline on its own: what
the Resend webhook kicks off for each email it announces, after the response
has gone back.

The receipt is locked for the run, so a redelivered webhook (or the admin
process route) landing on the same email at the same moment steps aside rather
than importing the booking twice.
"""
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailReceipt
from app.services.mail_extraction import MailExtractor
from app.services.mail_processing import extract_receipt
from app.services.receipt_import import ImportOutcome, process_receipt
from app.services.resend import ResendClient

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]


def process_email(
    session: Session, email_id: str, *, resend: ResendClient, extractor: MailExtractor
) -> ImportOutcome | None:
    """Extract and import the stored email `email_id`. None when there is no
    such receipt or another run holds it. The caller commits."""
    receipt = session.scalar(
        select(EmailReceipt)
        .where(EmailReceipt.resend_email_id == email_id)
        .with_for_update(skip_locked=True)
    )
    if receipt is None:
        return None
    return process_receipt(
        session,
        receipt,
        extract=lambda: extract_receipt(session, receipt, resend=resend, extractor=extractor),
    )


def process_email_in_own_session(
    session_factory: SessionFactory,
    email_id: str,
    *,
    resend: ResendClient,
    extractor: MailExtractor,
) -> None:
    """`process_email` in a session of its own, committed. Runs after the
    webhook has answered, so nothing is raised: a failure is logged and the
    receipt keeps the status it had, for `POST /admin/received/mail/process`
    to pick up."""
    try:
        with session_factory() as session:
            try:
                outcome = process_email(session, email_id, resend=resend, extractor=extractor)
                session.commit()
            except Exception:
                session.rollback()
                raise
    except Exception:
        logger.exception("processing received email %s failed", email_id)
        return
    if outcome is not None:
        logger.info(
            "received email %s: %s (%s)", email_id, outcome.outcome, outcome.receipt.status.value
        )
