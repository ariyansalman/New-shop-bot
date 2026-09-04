"""order_items.product_id nullable so a sold product can be deleted

Revision ID: b7c41a9d2e10
Revises: 66b1e79055b5
Create Date: 2026-09-01 09:20:00.000000

Deleting a product that had ever been ordered raised
"IntegrityError: NOT NULL constraint failed: order_items.product_id":
SQLAlchemy nulls the child FK when the parent of a plain (non-cascading)
relationship is deleted, which the NOT NULL column rejected. The admin
saw nothing at all, because the handler had already answered the callback
query, so the global error handler's fallback message could not be sent
either.

Making the column nullable is the fix the rest of the codebase was
already written for - both order-detail views render a missing product as
"Deleted product" / "Unknown Product" - and it keeps the order line (its
quantity, the price actually paid, and the delivered keys/link) as the
customer's receipt.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c41a9d2e10'
down_revision: Union[str, None] = '66b1e79055b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.alter_column(
            'product_id',
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    # Rows whose product was deleted have product_id IS NULL and cannot be
    # restored to NOT NULL. Drop them so the constraint can be re-applied -
    # the same information loss the NOT NULL column implied in the first
    # place, made explicit here rather than failing halfway through.
    op.execute(sa.text("DELETE FROM order_items WHERE product_id IS NULL"))

    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.alter_column(
            'product_id',
            existing_type=sa.Integer(),
            nullable=False,
        )
