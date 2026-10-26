"""trip item participants by member: RSVPs point at trip_members, not users

So an organizer can record an answer for someone on the trip who has no
account. Existing rows are matched to the member row for the same user on the
item's trip. A row with no such member (someone no longer on the trip, which
member removal already cleans up) has nothing to point at and is dropped.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5e6f7a8b9c0d'
down_revision: Union[str, None] = '4d5e6f7a8b9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trip_item_participants', sa.Column('member_id', sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE trip_item_participants AS p
        SET member_id = m.id
        FROM trip_items AS i, trip_members AS m
        WHERE i.id = p.item_id AND m.trip_id = i.trip_id AND m.user_id = p.user_id
        """
    )
    op.execute('DELETE FROM trip_item_participants WHERE member_id IS NULL')

    op.drop_constraint('pk_trip_item_participants', 'trip_item_participants', type_='primary')
    op.drop_index(op.f('ix_trip_item_participants_user_id'), table_name='trip_item_participants')
    op.drop_constraint(
        'fk_trip_item_participants_user_id_users', 'trip_item_participants', type_='foreignkey'
    )
    op.drop_column('trip_item_participants', 'user_id')

    op.alter_column('trip_item_participants', 'member_id', nullable=False)
    op.create_foreign_key(
        op.f('fk_trip_item_participants_member_id_trip_members'), 'trip_item_participants',
        'trip_members', ['member_id'], ['id'], ondelete='CASCADE',
    )
    op.create_primary_key(
        'pk_trip_item_participants', 'trip_item_participants', ['item_id', 'member_id']
    )
    op.create_index(
        op.f('ix_trip_item_participants_member_id'), 'trip_item_participants', ['member_id'],
        unique=False,
    )


def downgrade() -> None:
    # The old shape needs an account per row: answers recorded for members
    # without one can't be represented and are dropped.
    op.add_column('trip_item_participants', sa.Column('user_id', sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE trip_item_participants AS p
        SET user_id = m.user_id
        FROM trip_members AS m
        WHERE m.id = p.member_id
        """
    )
    op.execute('DELETE FROM trip_item_participants WHERE user_id IS NULL')

    op.drop_index(
        op.f('ix_trip_item_participants_member_id'), table_name='trip_item_participants'
    )
    op.drop_constraint('pk_trip_item_participants', 'trip_item_participants', type_='primary')
    op.drop_constraint(
        'fk_trip_item_participants_member_id_trip_members', 'trip_item_participants',
        type_='foreignkey',
    )
    op.drop_column('trip_item_participants', 'member_id')

    op.alter_column('trip_item_participants', 'user_id', nullable=False)
    op.create_foreign_key(
        op.f('fk_trip_item_participants_user_id_users'), 'trip_item_participants', 'users',
        ['user_id'], ['id'], ondelete='CASCADE',
    )
    op.create_primary_key(
        'pk_trip_item_participants', 'trip_item_participants', ['item_id', 'user_id']
    )
    op.create_index(
        op.f('ix_trip_item_participants_user_id'), 'trip_item_participants', ['user_id'],
        unique=False,
    )
