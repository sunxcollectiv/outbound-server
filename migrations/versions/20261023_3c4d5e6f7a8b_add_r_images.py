"""add r_images table (uploaded images in the received-item pipeline)

Standalone from r_email_receipts: own table, reusing the status lifecycle and the
extracted/import fields. Image bytes stored inline (bytea). The uploader is the
owner (user_id, not null).

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, None] = '2b3c4d5e6f7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS = ('received', 'pending', 'processing', 'extracted', 'processed', 'rejected', 'ignored')


def upgrade() -> None:
    op.create_table(
        'r_images',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('trip_id', sa.Uuid(), nullable=True),
        sa.Column(
            'status',
            sa.Enum(*_STATUS, name='received_status', native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('extracted', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('extracted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failures', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('received', 'pending', 'processing', 'extracted', 'processed', 'rejected', 'ignored')",
            name=op.f('ck_r_images_received_status'),
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_r_images_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], name=op.f('fk_r_images_trip_id_trips'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_r_images')),
    )
    op.create_index(op.f('ix_r_images_user_id'), 'r_images', ['user_id'], unique=False)
    op.create_index(op.f('ix_r_images_trip_id'), 'r_images', ['trip_id'], unique=False)
    op.create_index(op.f('ix_r_images_sha256'), 'r_images', ['sha256'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_r_images_sha256'), table_name='r_images')
    op.drop_index(op.f('ix_r_images_trip_id'), table_name='r_images')
    op.drop_index(op.f('ix_r_images_user_id'), table_name='r_images')
    op.drop_table('r_images')
