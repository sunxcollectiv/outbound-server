"""add email_receipts table

Tracks inbound emails we've seen, keyed by their Resend id, so the same email is
never processed twice. Written receipt-on-sight (status=seen) from the manual
pull today and reusable by an email.received webhook later — both carry the same
resend_email_id, and the unique constraint on it is the dedup guard.

Revision ID: c5d9e2f3a4b6
Revises: b2c3d4e5f6a7
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c5d9e2f3a4b6'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'r_email_receipts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('resend_email_id', sa.String(length=255), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'processing', 'processed', 'rejected', 'ignored', name='email_receipt_status', native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('from_address', sa.String(length=320), nullable=True),
        sa.Column('subject', sa.String(length=1024), nullable=True),
        sa.Column('message_id', sa.String(length=998), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failures', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'rejected', 'ignored')",
            name=op.f('ck_r_email_receipts_email_receipt_status'),
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_r_email_receipts_user_id_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_r_email_receipts')),
        sa.UniqueConstraint('resend_email_id', name=op.f('uq_r_email_receipts_resend_email_id')),
    )
    op.create_index(op.f('ix_r_email_receipts_user_id'), 'r_email_receipts', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_r_email_receipts_user_id'), table_name='r_email_receipts')
    op.drop_table('r_email_receipts')
