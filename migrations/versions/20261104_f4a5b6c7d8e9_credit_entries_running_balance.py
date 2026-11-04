"""credit entries carry a running balance, chained per user

Until now a balance was the sum of a user's rows, recomputed on every read
and every charge. Each row now stores the balance right after it
(`balance_after_cents`), and names the row before it (`previous_id`) along
with that row's balance (`previous_balance_cents`), so the database can hold
the chain to account: a composite foreign key ties the pair to the real
previous row of the same user, a check makes balance_after = previous +
amount and never negative, `previous_id` is unique (no row built on twice)
and each user has one first row.

Existing rows are backfilled in `seq` order per user. Nothing is dropped.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('credit_entries', sa.Column('previous_id', sa.Uuid(), nullable=True))
    op.add_column('credit_entries', sa.Column('previous_balance_cents', sa.Integer(), nullable=True))
    op.add_column('credit_entries', sa.Column('balance_after_cents', sa.Integer(), nullable=True))

    op.execute(
        """
        UPDATE credit_entries AS e
        SET balance_after_cents = w.after,
            previous_id = w.previous_id,
            previous_balance_cents = w.previous_after
        FROM (
            SELECT
                id,
                sum(amount_cents) OVER (PARTITION BY user_id ORDER BY seq) AS after,
                lag(id) OVER (PARTITION BY user_id ORDER BY seq) AS previous_id,
                sum(amount_cents) OVER (
                    PARTITION BY user_id ORDER BY seq
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) AS previous_after
            FROM credit_entries
        ) AS w
        WHERE w.id = e.id
        """
    )
    op.alter_column('credit_entries', 'balance_after_cents', nullable=False)

    # Bare tokens: the naming convention makes these ck_credit_entries_<token>.
    op.create_check_constraint(
        'balance_after_adds_up',
        'credit_entries',
        'balance_after_cents = COALESCE(previous_balance_cents, 0) + amount_cents',
    )
    op.create_check_constraint(
        'balance_after_not_negative', 'credit_entries', 'balance_after_cents >= 0'
    )
    op.create_check_constraint(
        'previous_balance_with_previous',
        'credit_entries',
        '(previous_id IS NULL) = (previous_balance_cents IS NULL)',
    )
    op.create_unique_constraint(
        op.f('uq_credit_entries_chain_link'),
        'credit_entries',
        ['user_id', 'id', 'balance_after_cents'],
    )
    op.create_foreign_key(
        op.f('fk_credit_entries_previous_credit_entries'),
        'credit_entries',
        'credit_entries',
        ['user_id', 'previous_id', 'previous_balance_cents'],
        ['user_id', 'id', 'balance_after_cents'],
    )
    op.create_unique_constraint(
        op.f('uq_credit_entries_previous_id'), 'credit_entries', ['previous_id']
    )
    op.create_index(
        'uq_credit_entries_user_id_first',
        'credit_entries',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text('previous_id IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_credit_entries_user_id_first', table_name='credit_entries')
    op.drop_constraint(op.f('uq_credit_entries_previous_id'), 'credit_entries', type_='unique')
    op.drop_constraint(
        op.f('fk_credit_entries_previous_credit_entries'), 'credit_entries', type_='foreignkey'
    )
    op.drop_constraint(op.f('uq_credit_entries_chain_link'), 'credit_entries', type_='unique')
    op.drop_constraint('previous_balance_with_previous', 'credit_entries', type_='check')
    op.drop_constraint('balance_after_not_negative', 'credit_entries', type_='check')
    op.drop_constraint('balance_after_adds_up', 'credit_entries', type_='check')
    op.drop_column('credit_entries', 'balance_after_cents')
    op.drop_column('credit_entries', 'previous_balance_cents')
    op.drop_column('credit_entries', 'previous_id')
