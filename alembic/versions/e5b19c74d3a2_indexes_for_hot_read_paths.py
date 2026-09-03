"""Index the columns the busiest screens filter and sort on

Revision ID: e5b19c74d3a2
Revises: d9f47b23e6a1
Create Date: 2026-09-03

Seven columns carried no index while being the filter or sort key of a
screen that runs constantly:

  products.category_id / subcategory_id / is_active
      Every catalogue browse and every availability build scans the whole
      product table once per category.
  orders.status, orders.created_at
      The admin dashboard runs four status aggregates on open, and every
      order list sorts by created_at.
  product_keys.order_id
      Read when an order is cancelled and its keys are returned to stock.
  transactions.created_at
      The sort key of the admin transaction list.

Indexes only, no data or shape change, so this is safe to apply to a live
database and reversible without loss.
"""

from alembic import op

revision = 'e5b19c74d3a2'
down_revision = 'd9f47b23e6a1'
branch_labels = None
depends_on = None


_INDEXES = [
    ('ix_products_category_id', 'products', ['category_id']),
    ('ix_products_subcategory_id', 'products', ['subcategory_id']),
    ('ix_products_is_active', 'products', ['is_active']),
    ('ix_orders_status', 'orders', ['status']),
    ('ix_orders_created_at', 'orders', ['created_at']),
    ('ix_product_keys_order_id', 'product_keys', ['order_id']),
    ('ix_transactions_created_at', 'transactions', ['created_at']),
]


def _existing(table: str) -> set:
    """Index names already on the table.

    A deployment whose schema was built by create_all() on current models
    already has these, so creating them again would fail. Checking is
    cheaper than making the boot repair special-case this revision.
    """
    from sqlalchemy import inspect

    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index['name'] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    seen = {}
    for name, table, columns in _INDEXES:
        if table not in seen:
            seen[table] = _existing(table)
        if name not in seen[table]:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        if name in _existing(table):
            op.drop_index(name, table_name=table)
