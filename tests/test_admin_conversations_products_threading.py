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


async def test_edit_select_field_delete_actually_removes_sold_keys_too():
    """Documents actual behavior, not the code's own claim.

    handle_select_field's "edit_delete" branch explicitly bulk-deletes only
    unsold keys before session.delete(product), with a comment saying sold
    keys "remain for order history" - but Product.product_keys carries
    cascade="all, delete-orphan" (database/models.py), so the ORM's
    session.delete(product) cascades and removes every ProductKey row for
    that product anyway, sold ones included. This is a pre-existing bug
    (present before this refactor, unrelated to asyncio.to_thread), not a
    regression - this test pins the real behavior so a future fix has a
    test to flip red->green against, rather than silently leaving both the
    comment and the assumption of preserved order-history evidence wrong.
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
