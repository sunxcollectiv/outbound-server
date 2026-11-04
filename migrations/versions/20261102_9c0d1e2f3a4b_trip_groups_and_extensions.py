"""trips: groups and extension links

A trip can be created by extending another; the extension is a full trip of
its own. Two new columns on trips record the relation. `extends_trip_id` is
the directed provenance (which trip this one was extended from; nulled if
that trip goes). `group_id` is the undirected cluster both belong to, backed
by the new `trip_groups` table so it can grow a name and a thread later;
it outlives any one trip in the cluster. Standalone trips have both null.

Only adds: nothing existing is touched. Downgrade drops the links; the
trips themselves stay.

Revision ID: 9c0d1e2f3a4b
Revises: d2e3f4a5b6c7
Create Date: 2026-11-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9c0d1e2f3a4b'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trip_groups',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trip_groups')),
    )

    op.add_column('trips', sa.Column('group_id', sa.Uuid(), nullable=True))
    op.add_column('trips', sa.Column('extends_trip_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_trips_group_id'), 'trips', ['group_id'], unique=False)
    op.create_index(op.f('ix_trips_extends_trip_id'), 'trips', ['extends_trip_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_trips_group_id_trip_groups'), 'trips', 'trip_groups',
        ['group_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_trips_extends_trip_id_trips'), 'trips', 'trips',
        ['extends_trip_id'], ['id'], ondelete='SET NULL',
    )
    # Bare names: create_check_constraint applies the naming convention itself.
    op.create_check_constraint(
        'extension_in_group', 'trips', 'extends_trip_id IS NULL OR group_id IS NOT NULL'
    )
    op.create_check_constraint('extension_not_self', 'trips', 'extends_trip_id IS DISTINCT FROM id')


def downgrade() -> None:
    op.drop_constraint('extension_not_self', 'trips', type_='check')
    op.drop_constraint('extension_in_group', 'trips', type_='check')
    op.drop_constraint(op.f('fk_trips_extends_trip_id_trips'), 'trips', type_='foreignkey')
    op.drop_constraint(op.f('fk_trips_group_id_trip_groups'), 'trips', type_='foreignkey')
    op.drop_index(op.f('ix_trips_extends_trip_id'), table_name='trips')
    op.drop_index(op.f('ix_trips_group_id'), table_name='trips')
    op.drop_column('trips', 'extends_trip_id')
    op.drop_column('trips', 'group_id')
    op.drop_table('trip_groups')
