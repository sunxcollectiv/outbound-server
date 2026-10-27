"""trip items: participants + visibility replace group/personal scope

An item is now described by who it's for (its participant rows, always
explicit: no implicit "everyone") and what non-participants see (visibility:
private / open / closed). Participant status becomes optional.

Existing data: a group item becomes open with every current trip member as a
participant (their existing answers kept, the rest with no answer); personal
items keep their participants, `shared` and `open` both becoming open and
`private` staying private. requirement stays, now optional on any item.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-10-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6f7a8b9c0d1e'
down_revision: Union[str, None] = '5e6f7a8b9c0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Check constraints go by bare token: the naming convention renders
# ck_trip_items_<token> on both create and drop.
_OLD_VISIBILITY = "visibility IN ('private', 'shared', 'open')"
_NEW_VISIBILITY = "visibility IN ('private', 'open', 'closed')"


def upgrade() -> None:
    op.alter_column('trip_item_participants', 'status', nullable=True)

    op.drop_constraint('group_has_requirement', 'trip_items', type_='check')
    op.drop_constraint('personal_has_visibility', 'trip_items', type_='check')
    op.drop_constraint('trip_item_visibility', 'trip_items', type_='check')

    op.execute(
        """
        INSERT INTO trip_item_participants (item_id, member_id, status)
        SELECT i.id, m.id, NULL
        FROM trip_items AS i
        JOIN trip_members AS m ON m.trip_id = i.trip_id
        WHERE i.scope = 'group'
          AND NOT EXISTS (
            SELECT 1 FROM trip_item_participants AS p
            WHERE p.item_id = i.id AND p.member_id = m.id
          )
        """
    )
    op.execute(
        "UPDATE trip_items SET visibility = 'open' "
        "WHERE scope = 'group' OR visibility = 'shared'"
    )
    op.alter_column('trip_items', 'visibility', nullable=False)
    op.create_check_constraint('trip_item_visibility', 'trip_items', _NEW_VISIBILITY)

    op.drop_constraint('trip_item_scope', 'trip_items', type_='check')
    op.drop_column('trip_items', 'scope')


def downgrade() -> None:
    # Best effort: an item listing every trip member (or with no creator left)
    # goes back to group, anything else to personal. Group/personal-only
    # fields are filled in or cleared to fit, and missing answers become going.
    op.add_column(
        'trip_items',
        sa.Column(
            'scope',
            sa.Enum('group', 'personal', name='trip_item_scope', native_enum=False, length=32),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE trip_items AS i
        SET scope = CASE
            WHEN i.created_by_id IS NULL OR (
                SELECT count(*) FROM trip_item_participants AS p WHERE p.item_id = i.id
            ) >= (
                SELECT count(*) FROM trip_members AS m WHERE m.trip_id = i.trip_id
            ) THEN 'group'
            ELSE 'personal'
        END
        """
    )
    op.alter_column('trip_items', 'scope', nullable=False)
    op.create_check_constraint('trip_item_scope', 'trip_items', "scope IN ('group', 'personal')")

    op.drop_constraint('trip_item_visibility', 'trip_items', type_='check')
    op.alter_column('trip_items', 'visibility', nullable=True)
    op.execute(
        """
        UPDATE trip_items SET
            requirement = CASE WHEN scope = 'group' THEN coalesce(requirement, 'optional') END,
            visibility = CASE
                WHEN scope = 'group' THEN NULL
                WHEN visibility = 'private' THEN 'private'
                ELSE 'shared'
            END
        """
    )
    op.create_check_constraint('trip_item_visibility', 'trip_items', _OLD_VISIBILITY)
    op.create_check_constraint(
        'group_has_requirement', 'trip_items', "(scope = 'group') = (requirement IS NOT NULL)"
    )
    op.create_check_constraint(
        'personal_has_visibility', 'trip_items', "(scope = 'personal') = (visibility IS NOT NULL)"
    )

    op.execute("UPDATE trip_item_participants SET status = 'going' WHERE status IS NULL")
    op.alter_column('trip_item_participants', 'status', nullable=False)
