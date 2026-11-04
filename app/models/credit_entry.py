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
    Uuid,
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
    their rows. Credits are US cents. Rows are never edited or deleted, and
    they outlive everything they refer to: `trip_id` is a plain id (no
    foreign key) that stays once the trip is gone, with the trip's title
    kept in `description`; `user_id` goes null if the account is deleted,
    so the history of a trip's payments survives its payers. A refund is its
    own row pointing at the charge it draws down (`refund_of_id`), so a
    trip's charges and refunds can always be matched up per payer, whoever
    the refund was paid to.

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
        CheckConstraint(
            "refund_of_id IS NULL OR kind = 'trip_refund'", name="refund_of_only_on_refunds"
        ),
        Index("ix_credit_entries_user_id_seq", "user_id", text("seq DESC")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    kind: Mapped[CreditEntryKind] = mapped_column(str_enum(CreditEntryKind, "credit_entry_kind"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    # For a refund: the charge it draws down. Charges are never deleted.
    refund_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_entries.id"))
    external_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # The trip's title at the time, or an admin's note on a grant.
    description: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
