"""handlers.admin_conversations.products - regression tests for the
asyncio.to_thread refactor. Covers the highest-risk branches: product
creation (dedup + stock_count), and edit_select_field's several
independent early-return branches (activate/deactivate/clear_keys/
delete), plus edit_new_value's price/category updates.
"""

from decimal import Decimal

from database import get_db_session, Category, Product, ProductKey, ProductType
from handlers.admin_conversations.products import (
    create_product_final, edit_select_field, edit_new_value,
)
from telegram.ext import ConversationHandler
from fakes import FakeUpdate, FakeQuery, FakeContext, FakeMessage, FakeMessageUpdate

ADMIN_ID = 1000000001  # matches conftest.py's ADMIN_TELEGRAM_ID


def make_category(name="Games") -> int:
    with get_db_session() as session:
        cat = Category(name=name)
        session.add(cat)
        session.flush()
        return cat.id


def make_product(name="Widget", price="9.99", stock=5, product_type=ProductType.KEY) -> int:
    with get_db_session() as session:
        product = Product(name=name, price=Decimal(price), stock_count=stock, product_type=product_type, is_active=True)
        session.add(product)
        session.flush()
        return product.id


async def test_create_product_final_dedupes_keys_and_sets_stock():
    category_id = make_category()
    message = FakeMessage()
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data.update({
        'product_name': "Steam Key",
        'product_desc': "A game key",
        'product_price': Decimal("19.99"),
        'product_type': ProductType.KEY,
        'product_category': category_id,
        'product_subcategory': None,
        'product_image': None,
        'product_download_link': None,
        'product_keys': ["KEY-1", "KEY-2", "KEY-1"],  # KEY-1 duplicated
    })

    await create_product_final(update, context)

    assert "Successfully" in message.last_reply_text
    assert "Duplicates dropped: 1" in message.last_reply_text
    with get_db_session() as session:
        product = session.query(Product).filter_by(name="Steam Key").first()
        assert product.stock_count == 2
        assert session.query(ProductKey).filter_by(product_id=product.id).count() == 2
    assert context.user_data == {}


async def test_create_product_final_file_type_gets_unlimited_stock():
    category_id = make_category()
    message = FakeMessage()
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data.update({
        'product_name': "Ebook",
        'product_desc': "A PDF",
        'product_price': Decimal("4.99"),
        'product_type': ProductType.FILE,
        'product_category': category_id,
        'product_subcategory': None,
        'product_image': None,
        'product_download_link': "https://example.com/file.pdf",
        'product_keys': [],
    })

    await create_product_final(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(name="Ebook").first()
        assert product.stock_count == 999999
        assert product.download_link == "https://example.com/file.pdf"


async def test_edit_select_field_activate():
    product_id = make_product()
    with get_db_session() as session:
        session.query(Product).filter_by(id=product_id).update({"is_active": False})

    query = FakeQuery(data="edit_activate", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id

    result = await edit_select_field(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.is_active is True
    assert "activated" in query.last_edit_text
    assert context.user_data == {}
    assert result == ConversationHandler.END


async def test_edit_select_field_clear_keys_removes_unsold_only():
    product_id = make_product(stock=2)
    with get_db_session() as session:
        session.add(ProductKey(product_id=product_id, key_value="K1", is_sold=False))
        session.add(ProductKey(product_id=product_id, key_value="K2", is_sold=True))

    query = FakeQuery(data="edit_clear_keys", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id

    await edit_select_field(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.stock_count == 0
        remaining = session.query(ProductKey).filter_by(product_id=product_id).all()
        assert len(remaining) == 1
        assert remaining[0].is_sold is True  # sold key untouched

    assert "Cleared 1 unsold" in query.last_edit_text


async def test_edit_select_field_clear_keys_noop_when_none_unsold():
    product_id = make_product(stock=0)
    query = FakeQuery(data="edit_clear_keys", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id

    await edit_select_field(update, context)

    assert "no unsold keys" in query.last_edit_text


async def test_edit_select_field_delete_removes_key_inventory():
    """The whole key inventory goes with the product, sold rows included.

    Product.product_keys cascades delete-orphan, so session.delete(product)
    removes every ProductKey row regardless - and that is fine: the keys a
    customer actually received were copied into OrderItem.delivered_asset
    at sale time (see confirm_purchase), which is what order history
    renders from. These rows are inventory bookkeeping, not the receipt.
    """
    product_id = make_product()
    with get_db_session() as session:
        session.add(ProductKey(product_id=product_id, key_value="SOLD", is_sold=True))

    query = FakeQuery(data="edit_delete", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id

    await edit_select_field(update, context)

    with get_db_session() as session:
        assert session.query(Product).filter_by(id=product_id).first() is None
        assert session.query(ProductKey).filter_by(product_id=product_id).count() == 0

    assert "deleted successfully" in query.last_edit_text


async def test_delete_product_with_order_history_keeps_the_receipt():
    """Regression: this used to crash and silently do nothing.

    session.delete(product) made SQLAlchemy null out order_items.product_id,
    which was NOT NULL -> IntegrityError. The handler had already called
    query.answer(), so the global error handler could not answer it a
    second time either and the admin saw no message at all - the button
    just looked dead for any product that had ever been sold.

    order_items.product_id is nullable now (migration b7c41a9d2e10) and the
    handler detaches the lines explicitly, so the order line survives as
    the customer's receipt with its quantity, price paid and delivered keys.
    """
    from database import User, Order, OrderItem, OrderStatus

    with get_db_session() as session:
        user = User(telegram_id=424242, wallet_balance=Decimal("0"))
        session.add(user)
        session.flush()
        product = Product(
            name="Sold Widget", price=Decimal("9.99"), stock_count=1,
            product_type=ProductType.KEY, is_active=True,
        )
        session.add(product)
        session.flush()
        order = Order(user_id=user.id, total_amount=Decimal("9.99"), status=OrderStatus.COMPLETED)
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=1,
            price=Decimal("9.99"), delivered_asset="KEY-ABC-123",
        ))
        session.add(ProductKey(product_id=product.id, key_value="KEY-ABC-123",
                               is_sold=True, order_id=order.id))
        product_id, order_id = product.id, order.id

    query = FakeQuery(data="edit_delete", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id

    await edit_select_field(update, context)

    # The admin gets a real confirmation, not silence.
    assert "deleted successfully" in query.last_edit_text

    with get_db_session() as session:
        assert session.query(Product).filter_by(id=product_id).first() is None

        item = session.query(OrderItem).filter_by(order_id=order_id).one()
        assert item.product_id is None          # detached, not deleted
        assert item.price == Decimal("9.99")    # what the customer paid
        assert item.quantity == 1
        assert item.delivered_asset == "KEY-ABC-123"  # what they received


async def test_edit_new_value_price_updates_with_audit_log():
    from database import AdminActionLog
    product_id = make_product(price="9.99")
    message = FakeMessage(text="24.50")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id
    context.user_data['edit_field'] = 'price'

    result = await edit_new_value(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.price == Decimal("24.50")
        assert session.query(AdminActionLog).filter_by(action="edit_product_price").count() == 1

    assert "updated successfully" in message.last_reply_text
    assert result == ConversationHandler.END


async def test_edit_new_value_invalid_price_reprompts():
    product_id = make_product(price="9.99")
    message = FakeMessage(text="not-a-number")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id
    context.user_data['edit_field'] = 'price'

    from handlers.admin_conversations.products import EDIT_NEW_VALUE
    result = await edit_new_value(update, context)

    assert result == EDIT_NEW_VALUE
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.price == Decimal("9.99")  # unchanged


async def test_edit_new_value_category_reassignment_clears_mismatched_subcategory():
    from database import Subcategory
    old_cat = make_category("Old")
    new_cat = make_category("New")
    with get_db_session() as session:
        sub = Subcategory(name="OldSub", category_id=old_cat)
        session.add(sub)
        session.flush()
        sub_id = sub.id
    product_id = make_product()
    with get_db_session() as session:
        session.query(Product).filter_by(id=product_id).update({"category_id": old_cat, "subcategory_id": sub_id})

    query = FakeQuery(data=f"newprodcat_{new_cat}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_product_id'] = product_id
    context.user_data['edit_field'] = 'category'

    await edit_new_value(update, context)

    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        assert product.category_id == new_cat
        assert product.subcategory_id is None  # cleared: didn't belong to new_cat
