"""handlers.admin_conversations.categories - regression tests for the
asyncio.to_thread refactor. Covers category/subcategory creation, the
delete branches (unlink-not-cascade behavior), and the subcategory
category-reassignment branch (the one with interleaved callback-query
handling that got restructured the most).
"""

from database import get_db_session, Category, Subcategory, Product, ProductType
from decimal import Decimal
from handlers.admin_conversations.categories import (
    category_desc, subcategory_name, edit_category_field, edit_subcategory_field,
    edit_subcategory_value,
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


def make_subcategory(category_id, name="Steam") -> int:
    with get_db_session() as session:
        sub = Subcategory(name=name, category_id=category_id)
        session.add(sub)
        session.flush()
        return sub.id


def make_product(category_id=None, subcategory_id=None, name="Widget") -> int:
    with get_db_session() as session:
        product = Product(
            name=name, price=Decimal("9.99"), stock_count=5, product_type=ProductType.KEY,
            is_active=True, category_id=category_id, subcategory_id=subcategory_id,
        )
        session.add(product)
        session.flush()
        return product.id


async def test_category_desc_creates_category():
    message = FakeMessage(text="A description")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['category_name'] = "Games"

    result = await category_desc(update, context)

    with get_db_session() as session:
        cat = session.query(Category).filter_by(name="Games").first()
        assert cat is not None
        assert cat.description == "A description"

    assert "created successfully" in message.last_reply_text
    assert result == ConversationHandler.END


async def test_subcategory_name_creates_under_parent():
    category_id = make_category("Games")
    message = FakeMessage(text="Steam")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['subcategory_category'] = category_id

    await subcategory_name(update, context)

    with get_db_session() as session:
        sub = session.query(Subcategory).filter_by(name="Steam").first()
        assert sub is not None
        assert sub.category_id == category_id

    assert "created under 'Games'" in message.last_reply_text


async def test_edit_category_delete_unlinks_products_and_subcategories():
    category_id = make_category()
    make_subcategory(category_id, "Sub1")
    product_id = make_product(category_id=category_id)

    query = FakeQuery(data="editcat_delete", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_category_id'] = category_id

    await edit_category_field(update, context)

    with get_db_session() as session:
        assert session.query(Category).filter_by(id=category_id).first() is None
        product = session.query(Product).filter_by(id=product_id).first()
        assert product is not None  # not deleted
        assert product.category_id is None  # unlinked

    assert "1 product(s)" in query.last_edit_text
    assert "1 subcategory(ies)" in query.last_edit_text


async def test_edit_subcategory_delete_unlinks_products_not_deletes():
    category_id = make_category()
    subcategory_id = make_subcategory(category_id)
    product_id = make_product(subcategory_id=subcategory_id)

    query = FakeQuery(data="editsubcat_delete", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_subcategory_id'] = subcategory_id

    await edit_subcategory_field(update, context)

    with get_db_session() as session:
        assert session.query(Subcategory).filter_by(id=subcategory_id).first() is None
        product = session.query(Product).filter_by(id=product_id).first()
        assert product is not None
        assert product.subcategory_id is None

    assert "1 product(s) remain" in query.last_edit_text


async def test_edit_subcategory_value_reassigns_category():
    old_cat = make_category("Old")
    new_cat = make_category("New")
    subcategory_id = make_subcategory(old_cat)

    query = FakeQuery(data=f"newcat_{new_cat}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_subcategory_id'] = subcategory_id
    context.user_data['edit_subcategory_field'] = 'category'

    result = await edit_subcategory_value(update, context)

    with get_db_session() as session:
        sub = session.query(Subcategory).filter_by(id=subcategory_id).first()
        assert sub.category_id == new_cat

    assert "updated successfully" in query.last_edit_text
    assert result == ConversationHandler.END


async def test_edit_subcategory_value_cancel_leaves_category_unchanged():
    old_cat = make_category("Old")
    subcategory_id = make_subcategory(old_cat)

    query = FakeQuery(data="cancel_edit_subcat", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()
    context.user_data['edit_subcategory_id'] = subcategory_id
    context.user_data['edit_subcategory_field'] = 'category'

    await edit_subcategory_value(update, context)

    with get_db_session() as session:
        sub = session.query(Subcategory).filter_by(id=subcategory_id).first()
        assert sub.category_id == old_cat  # unchanged

    assert "cancelled" in query.last_edit_text.lower()
