"""credits ledger and trip price schedule

Trips are paid for in credits. `credit_entries` is the ledger: one signed
row (US cents) per movement for one user; a balance is the sum. Grants and
store purchases are positive, a trip charge is negative, a refund (paid to
the trip's owner, pointing at the charge it draws down) is positive. Rows
outlive what they refer to: trip_id is a plain id with no foreign key, and
user_id goes null with the account. `seq` is the insertion order, used to
list.

trips.price_schedule pins each trip to the rate table it was priced under
(app.services.pricing); existing trips get the current one, and their first
price-raising edit settles the full amount owed.

Only adds: nothing existing is touched beyond the new NOT NULL column with a
default.

Revision ID: e3f4a5b6c7d8
Revises: 9c0d1e2f3a4b
Create Date: 2026-11-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = '9c0d1e2f3a4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KINDS = ('grant', 'purchase', 'trip_charge', 'trip_refund')


def upgrade() -> None:
    op.add_column(
        'trips',
        sa.Column('price_schedule', sa.String(length=16), server_default='2026-11', nullable=False),
    )

    op.create_table(
        'credit_entries',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('trip_id', sa.Uuid(), nullable=True),
        sa.Column(
            'kind',
            sa.Enum(*_KINDS, name='credit_entry_kind', native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('refund_of_id', sa.Uuid(), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('description', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "kind IN ('grant', 'purchase', 'trip_charge', 'trip_refund')",
            name=op.f('ck_credit_entries_credit_entry_kind'),
        ),
        sa.CheckConstraint(
            "(kind = 'trip_charge' AND amount_cents < 0) OR (kind <> 'trip_charge' AND amount_cents > 0)",
            name=op.f('ck_credit_entries_amount_sign_matches_kind'),
        ),
        sa.CheckConstraint(
            "kind IN ('trip_charge', 'trip_refund') OR trip_id IS NULL",
            name=op.f('ck_credit_entries_trip_only_on_trip_kinds'),
        ),
        sa.CheckConstraint(
            "refund_of_id IS NULL OR kind = 'trip_refund'",
            name=op.f('ck_credit_entries_refund_of_only_on_refunds'),
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_credit_entries_user_id_users'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['refund_of_id'], ['credit_entries.id'],
            name=op.f('fk_credit_entries_refund_of_id_credit_entries'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_entries')),
        sa.UniqueConstraint('seq', name=op.f('uq_credit_entries_seq')),
        sa.UniqueConstraint('external_id', name=op.f('uq_credit_entries_external_id')),
    )
    op.create_index(op.f('ix_credit_entries_trip_id'), 'credit_entries', ['trip_id'])
    op.create_index(
        'ix_credit_entries_user_id_seq', 'credit_entries',
        ['user_id', sa.literal_column('seq DESC')],
    )


def downgrade() -> None:
    op.drop_index('ix_credit_entries_user_id_seq', table_name='credit_entries')
    op.drop_index(op.f('ix_credit_entries_trip_id'), table_name='credit_entries')
    op.drop_table('credit_entries')
    op.drop_column('trips', 'price_schedule')
