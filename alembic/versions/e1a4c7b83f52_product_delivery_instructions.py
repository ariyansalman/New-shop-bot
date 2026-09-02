"""product + order_item delivery instructions

Optional per-product text telling the buyer how to redeem what they just
bought. The order line keeps its own copy, taken at purchase time, so a
customer's receipt still reads correctly after the product's instructions
are edited or the product is deleted.

Revision ID: e1a4c7b83f52
Revises: d3f6b18c4a27
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1a4c7b83f52'
down_revision: Union[str, None] = 'd3f6b18c4a27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip whatever create_all() already built - see the same guard in
    # c8d5f2a41b93 and why database/db.py can reach a migration whose
    # columns are partly present.
    inspector = sa.inspect(op.get_bind())

    if 'delivery_instructions' not in {c["name"] for c in inspector.get_columns("products")}:
        with op.batch_alter_table('products') as batch_op:
            batch_op.add_column(sa.Column('delivery_instructions', sa.Text(), nullable=True))

    if 'delivery_instructions' not in {c["name"] for c in inspector.get_columns("order_items")}:
        with op.batch_alter_table('order_items') as batch_op:
            batch_op.add_column(sa.Column('delivery_instructions', sa.Text(), nullable=True))


def downgrade() -> None:
    # Nothing to preserve: the columns are additive and nullable, so older
    # code simply stops reading them.
    with op.batch_alter_table('order_items') as batch_op:
        batch_op.drop_column('delivery_instructions')
    with op.batch_alter_table('products') as batch_op:
        batch_op.drop_column('delivery_instructions')
