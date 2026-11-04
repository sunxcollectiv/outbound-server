from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
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
    is for store receipts, so a purchase can't be credited twice.

    Each user's rows form a chain that carries the running balance, so the
    balance is the newest row's `balance_after_cents` rather than a sum, and
    the database keeps it honest: a row names the row before it
    (`previous_id`, null on the first) and repeats that row's balance, a
    foreign key holds both to the real thing, a check makes
    `balance_after_cents` the previous balance plus `amount_cents` and never
    negative, and no row can be built on twice. Two writes that raced past
    the user's row lock fail rather than both spending the same balance.
    Once an account is deleted its rows (user_id null) keep their numbers
    but are no longer checked against each other."""

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
        CheckConstraint(
            "balance_after_cents = COALESCE(previous_balance_cents, 0) + amount_cents",
            name="balance_after_adds_up",
        ),
        CheckConstraint("balance_after_cents >= 0", name="balance_after_not_negative"),
        CheckConstraint(
            "(previous_id IS NULL) = (previous_balance_cents IS NULL)",
            name="previous_balance_with_previous",
        ),
        # The target of the chain's foreign key (id alone is already unique).
        UniqueConstraint(
            "user_id", "id", "balance_after_cents", name="uq_credit_entries_chain_link"
        ),
        ForeignKeyConstraint(
            ["user_id", "previous_id", "previous_balance_cents"],
            ["credit_entries.user_id", "credit_entries.id", "credit_entries.balance_after_cents"],
            name="fk_credit_entries_previous_credit_entries",
        ),
        # No forks: a row has at most one successor, a user one first row.
        UniqueConstraint("previous_id", name="uq_credit_entries_previous_id"),
        Index(
            "uq_credit_entries_user_id_first",
            "user_id",
            unique=True,
            postgresql_where=text("previous_id IS NULL"),
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
    # The chain: the user's row before this one, its balance, and the
    # balance once this row is counted.
    previous_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    previous_balance_cents: Mapped[int | None] = mapped_column(Integer)
    balance_after_cents: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    # The trip's title at the time, or an admin's note on a grant.
    description: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
