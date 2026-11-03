from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import UUIDPrimaryKeyMixin, str_enum


class CreditEntryKind(StrEnum):
    GRANT = "grant"  # issued by an admin; the only way in, for now
    PURCHASE = "purchase"  # bought through the store; external_id is the store's receipt
    TRIP_CHARGE = "trip_charge"  # spent on a trip (negative)
    TRIP_REFUND = "trip_refund"  # part of a charge given back (positive)


class CreditEntry(UUIDPrimaryKeyMixin, Base):
    """One movement of credits for one user; a user's balance is the sum of
    their rows. Credits are US cents. Rows are never edited or deleted: a
    refund is its own row, and a charge keeps its trip's title so the history
    still reads once the trip is gone (trip_id goes null then rather than
    taking the row with it).

    `seq` is the insertion order, for listing; `created_at` can't do that,
    since rows written in one transaction share a timestamp. `external_id`
    is for store receipts, so a purchase can't be credited twice."""

    __tablename__ = "credit_entries"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'trip_charge' AND amount_cents < 0)"
            " OR (kind <> 'trip_charge' AND amount_cents > 0)",
            name="amount_sign_matches_kind",
        ),
        CheckConstraint(
            "kind IN ('trip_charge', 'trip_refund') OR trip_id IS NULL",
            name="trip_only_on_trip_kinds",
        ),
        Index("ix_credit_entries_user_id_seq", "user_id", text("seq DESC")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[CreditEntryKind] = mapped_column(str_enum(CreditEntryKind, "credit_entry_kind"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # The trip's title at the time, or an admin's note on a grant.
    description: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
