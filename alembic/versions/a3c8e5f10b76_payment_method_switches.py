"""admin on/off switches for CryptoBot and Card

Binance already had one. These give the other two methods the same
control, so an admin can stop accepting a payment method during an
incident without a redeploy.

NULL means no admin has decided, in which case the method's environment
variable answers - so adding these columns changes nothing until someone
uses a switch.

Revision ID: a3c8e5f10b76
Revises: f2b9d61c8a74
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c8e5f10b76'
down_revision: Union[str, None] = 'f2b9d61c8a74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ('crypto_pay_enabled', 'card_pay_enabled')


def upgrade() -> None:
    # Skip whatever create_all() already built - see c8d5f2a41b93.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("settings")}
    with op.batch_alter_table('settings') as batch_op:
        for name in _COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('settings') as batch_op:
        for name in reversed(_COLUMNS):
            batch_op.drop_column(name)
