"""add r_email_receipts.raw and the `received` status

The retrieve stage (/received/mail) stores a received email with status
`received` and, for the single-email retrieve, the full body in `raw`. Both are
additive: a new status value (swap the CHECK) and a nullable JSONB column.

Revision ID: f9a0b1c2d3e4
Revises: e8f1a2b3c4d5
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e8f1a2b3c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "status IN ('pending', 'processing', 'processed', 'rejected', 'ignored')"
_NEW = "status IN ('received', 'pending', 'processing', 'processed', 'rejected', 'ignored')"


def upgrade() -> None:
    op.add_column(
        'r_email_receipts',
        sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Bare token 'email_receipt_status' -> ck_r_email_receipts_email_receipt_status.
    op.drop_constraint('email_receipt_status', 'r_email_receipts', type_='check')
    op.create_check_constraint('email_receipt_status', 'r_email_receipts', _NEW)


def downgrade() -> None:
    op.drop_constraint('email_receipt_status', 'r_email_receipts', type_='check')
    op.create_check_constraint('email_receipt_status', 'r_email_receipts', _OLD)
    op.drop_column('r_email_receipts', 'raw')
