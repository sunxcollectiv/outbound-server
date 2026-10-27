"""trip members by email: a member can exist before (or without) an account

trip_members gets its own id (user_id is no longer part of the key and becomes
nullable, linked once an account with the email exists) and the email the
person was added at. Existing rows are backfilled from their user's email.

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4d5e6f7a8b9c'
down_revision: Union[str, None] = '3c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'trip_members',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    )
    op.alter_column('trip_members', 'id', server_default=None)
    op.drop_constraint('pk_trip_members', 'trip_members', type_='primary')
    op.create_primary_key('pk_trip_members', 'trip_members', ['id'])

    op.add_column('trip_members', sa.Column('email', sa.String(length=320), nullable=True))
    op.execute(
        'UPDATE trip_members SET email = users.email FROM users WHERE users.id = trip_members.user_id'
    )
    op.alter_column('trip_members', 'email', nullable=False)
    op.create_index(op.f('ix_trip_members_email'), 'trip_members', ['email'], unique=False)

    op.alter_column('trip_members', 'user_id', nullable=True)
    op.drop_constraint('fk_trip_members_user_id_users', 'trip_members', type_='foreignkey')
    op.create_foreign_key(
        op.f('fk_trip_members_user_id_users'), 'trip_members', 'users',
        ['user_id'], ['id'], ondelete='SET NULL',
    )

    op.create_unique_constraint(
        'uq_trip_members_trip_id_email', 'trip_members', ['trip_id', 'email']
    )
    op.create_unique_constraint(
        'uq_trip_members_trip_id_user_id', 'trip_members', ['trip_id', 'user_id']
    )


def downgrade() -> None:
    # The old shape needs an account on every row: members added by email who
    # never signed up can't be represented and are dropped.
    op.execute('DELETE FROM trip_members WHERE user_id IS NULL')

    op.drop_constraint('uq_trip_members_trip_id_user_id', 'trip_members', type_='unique')
    op.drop_constraint('uq_trip_members_trip_id_email', 'trip_members', type_='unique')

    op.drop_constraint('fk_trip_members_user_id_users', 'trip_members', type_='foreignkey')
    op.create_foreign_key(
        op.f('fk_trip_members_user_id_users'), 'trip_members', 'users',
        ['user_id'], ['id'], ondelete='CASCADE',
    )
    op.alter_column('trip_members', 'user_id', nullable=False)

    op.drop_index(op.f('ix_trip_members_email'), table_name='trip_members')
    op.drop_column('trip_members', 'email')

    op.drop_constraint('pk_trip_members', 'trip_members', type_='primary')
    op.create_primary_key('pk_trip_members', 'trip_members', ['trip_id', 'user_id'])
    op.drop_column('trip_members', 'id')
