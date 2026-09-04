"""handlers.admin_handlers.handle_restock_keys_paste

Regression test for a stock double-counting bug found while adding the
audit log: stock_count was computed as
`unsold_key_count() + added_count`, but session.query()'s implicit
autoflush already sends the just-added keys to the DB before the count
runs, so they were counted once by the query AND once again by the
`+ added_count` - every restock inflated the advertised stock by exactly
the number of keys just added.
"""

from decimal import Decimal

from database import get_db_session, Product, ProductKey, ProductType, AdminActionLog
from handlers.admin_handlers import handle_restock_keys_paste
from fakes import FakeMessage, FakeMessageUpdate, FakeContext

ADMIN_ID = 1000000001  # matches ADMIN_TELEGRAM_ID set in conftest.py


def make_product_with_unsold_keys(existing_key_count=2) -> int:
    with get_db_session() as session:
        product = Product(
            name="Restock Test", price=Decimal("5.00"), stock_count=existing_key_count,
            product_type=ProductType.KEY, is_active=True,
        )
        session.add(product)
        session.flush()
        for i in range(existing_key_count):
            session.add(ProductKey(product_id=product.id, key_value=f"OLD-{i}", is_sold=False))
        return product.id


async def test_restock_stock_count_is_not_inflated():
    product_id = make_product_with_unsold_keys(existing_key_count=2)

    message = FakeMessage(text="NEW-1\nNEW-2\nNEW-3")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['restock_product_id'] = product_id

    await handle_restock_keys_paste(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        actual_unsold = session.query(ProductKey).filter_by(product_id=product_id, is_sold=False).count()

        assert actual_unsold == 5, "2 existing + 3 new"
        assert product.stock_count == 5, "must match the real count, not 8 (the double-counting bug)"

        log = session.query(AdminActionLog).filter_by(action="restock_keys").first()
        assert log is not None
        assert log.target_id == product_id


async def test_restock_skips_duplicate_keys():
    product_id = make_product_with_unsold_keys(existing_key_count=1)
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        # Rename the pre-existing key to something we'll "accidentally" repaste.
        key = session.query(ProductKey).filter_by(product_id=product_id).first()
        key.key_value = "DUP-KEY"

    message = FakeMessage(text="DUP-KEY\nFRESH-KEY")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['restock_product_id'] = product_id

    await handle_restock_keys_paste(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.stock_count == 2, "1 pre-existing + 1 genuinely new; the duplicate is skipped"
        assert session.query(ProductKey).filter_by(product_id=product_id, key_value="DUP-KEY").count() == 1

    assert "Skipped 1 duplicate" in message.last_reply_text
