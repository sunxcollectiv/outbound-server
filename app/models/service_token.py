import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import UUIDPrimaryKeyMixin


class ServiceToken(UUIDPrimaryKeyMixin, Base):
    """A machine-to-machine credential not tied to any user — e.g. the admin
    console (wravel-manage). Minted with scripts/issue_service_token.py. It
    authenticates the /admin APIs like a user's bearer token does, but resolves
    to a service principal (no `User`) and carries its own admin flag, so there
    is no fabricated user account behind it."""

    __tablename__ = "service_tokens"

    # Human label, e.g. "wravel-manage console".
    name: Mapped[str] = mapped_column(String(200))
    # SHA-256 of the bearer token; the raw token is only shown once, at creation.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Whether this token may call admin-only APIs. Today the only kind we mint.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Null = never expires, the usual case for a long-lived service token.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
