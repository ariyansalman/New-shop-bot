"""settings.binance_pay_enabled nullable = "no admin has decided"

The column was NOT NULL DEFAULT true, so a store that had never touched
the switch was indistinguishable from one an admin had deliberately
switched on. Combined with BINANCE_PAY_ENABLED also gating the method,
the panel's Enable button could never do anything: it refused while the
environment variable was off, and it cannot change an environment
variable.

NULL now means "follow BINANCE_PAY_ENABLED"; True/False means an admin
decided. Existing rows are reset to NULL: the switch shipped broken and
has never successfully been used, so there is no admin decision to lose.

Revision ID: f2b9d61c8a74
Revises: e1a4c7b83f52
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b9d61c8a74'
down_revision: Union[str, None] = 'e1a4c7b83f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        batch_op.alter_column('binance_pay_enabled',
                              existing_type=sa.Boolean(),
                              nullable=True,
                              server_default=None)
    op.execute(sa.text("UPDATE settings SET binance_pay_enabled = NULL"))


def downgrade() -> None:
    # NOT NULL again, so the NULLs need a value first. true matches the
    # column's original server default.
    op.execute(sa.text(
        "UPDATE settings SET binance_pay_enabled = true "
        "WHERE binance_pay_enabled IS NULL"))
    with op.batch_alter_table('settings') as batch_op:
        batch_op.alter_column('binance_pay_enabled',
                              existing_type=sa.Boolean(),
                              nullable=False,
                              server_default=sa.true())
