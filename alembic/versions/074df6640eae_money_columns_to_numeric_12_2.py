"""money columns to Numeric(12,2)

Float lets rounding error accumulate across repeated wallet debits/credits
(binary floats can't represent most decimal fractions exactly) and stores
values with no fixed precision. Numeric(12, 2) fixes both at the DB layer;
utils/money.py's to_money() is the matching fix on the Python side.

Revision ID: 074df6640eae
Revises: 96e65c626176
Create Date: 2026-09-01 06:22:14.584442

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '074df6640eae'
down_revision: Union[str, None] = '96e65c626176'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs that move from Float to Numeric(12, 2).
_MONEY_COLUMNS = [
    ("order_items", "price"),
    ("orders", "total_amount"),
    ("products", "price"),
    ("transactions", "amount"),
    ("users", "wallet_balance"),
]


def upgrade() -> None:
    for table, column in _MONEY_COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Float(),
                type_=sa.Numeric(precision=12, scale=2),
                existing_nullable=False,
                # Postgres has an assignment cast from float8 to numeric, but
                # spelling out the USING clause removes any doubt about
                # rounding behavior; ignored on SQLite, where batch mode
                # rebuilds the table instead of running a raw ALTER.
                postgresql_using=f"ROUND({column}::numeric, 2)",
            )


def downgrade() -> None:
    for table, column in reversed(_MONEY_COLUMNS):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Numeric(precision=12, scale=2),
                type_=sa.Float(),
                existing_nullable=False,
            )
