"""split extract from process: add `extracted` status, rename processed_at -> extracted_at

The extract stage now lands rows at `extracted` (structured data pulled); the
timestamp it sets is renamed to match. `processed`/`processed_at` are reserved
for the later step that turns an extracted email into trip item(s).

Revision ID: 1a2b3c4d5e6f
Revises: f9a0b1c2d3e4
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WITHOUT = "status IN ('received', 'pending', 'processing', 'processed', 'rejected', 'ignored')"
_WITH = "status IN ('received', 'pending', 'processing', 'extracted', 'processed', 'rejected', 'ignored')"


def upgrade() -> None:
    op.alter_column('r_email_receipts', 'processed_at', new_column_name='extracted_at')
    # Bare token 'email_receipt_status' -> ck_r_email_receipts_email_receipt_status.
    op.drop_constraint('email_receipt_status', 'r_email_receipts', type_='check')
    op.create_check_constraint('email_receipt_status', 'r_email_receipts', _WITH)


def downgrade() -> None:
    op.drop_constraint('email_receipt_status', 'r_email_receipts', type_='check')
    op.create_check_constraint('email_receipt_status', 'r_email_receipts', _WITHOUT)
    op.alter_column('r_email_receipts', 'extracted_at', new_column_name='processed_at')
