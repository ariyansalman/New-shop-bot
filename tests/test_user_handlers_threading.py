"""handlers.user_handlers - regression tests for the asyncio.to_thread
refactor (every DB-touching handler moved its query off the event loop).

These exist to catch exactly the class of bug that refactor risks: a value
that crossed the thread boundary as an ORM object instead of a plain tuple
(DetachedInstanceError on later attribute access), a mismatched tuple
unpack, or a branch whose early-return sentinel got mapped to the wrong
message. Driven through the real handler functions against a real (SQLite)
database, like tests/test_purchase_flow.py.
"""

from decimal import Decimal

from database import (
    get_db_session, User, Category, Subcategory, Product, Order, OrderItem,
    Settings, ProductType, OrderStatus, DisputeStatus,
)
from handlers.user_handlers import (
    start_command, main_menu_callback, set_language_callback,
    products_callback, category_callback, show_products_list,
    product_detail_callback, availability_callback, support_callback,
    order_history_callback, user_order_detail_callback,
)
from fakes import FakeUpdate, FakeQuery, FakeContext, FakeMessage, FakeMessageUpdate

TELEGRAM_ID = 777001


def make_user(balance="10.00", language="en") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance), language=language)
        session.add(user)
        session.flush()
        return user.id


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


def make_product(category_id=None, subcategory_id=None, name="Widget", price="9.99", stock=5) -> int:
    with get_db_session() as session:
        product = Product(
            name=name, price=Decimal(price), stock_count=stock,
            product_type=ProductType.KEY, is_active=True,
            category_id=category_id, subcategory_id=subcategory_id,
        )
        session.add(product)
        session.flush()
        return product.id


def make_order(user_id, total="19.99", status=OrderStatus.COMPLETED, dispute=DisputeStatus.NIL) -> int:
    with get_db_session() as session:
        order = Order(user_id=user_id, total_amount=Decimal(total), status=status, dispute_status=dispute)
        session.add(order)
        session.flush()
        return order.id


async def test_start_command_shows_wallet_balance_and_creates_user():
    message = FakeMessage()
    update = FakeMessageUpdate(message, TELEGRAM_ID)

    class _U:
        id = TELEGRAM_ID
        username = "tester"
    update.effective_user = _U()

    await start_command(update, FakeContext())

    assert "$0.00" in message.last_reply_text
    with get_db_session() as session:
        assert session.query(User).filter_by(telegram_id=TELEGRAM_ID).first() is not None


async def test_main_menu_callback_shows_balance():
    make_user(balance="42.50")
    query = FakeQuery(data="main_menu", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await main_menu_callback(update, FakeContext())

    assert "$42.50" in query.last_edit_text


async def test_set_language_callback_persists_choice():
    make_user(language="en")
    query = FakeQuery(data="set_lang_bn", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await set_language_callback(update, FakeContext())

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        assert user.language == "bn"
    # Bengali balance line should now be in the edited text.
    assert "ব্যালেন্স" in query.last_edit_text


async def test_set_language_callback_unknown_user_shows_error():
    query = FakeQuery(data="set_lang_bn", user_id=999999)
    update = FakeUpdate(query, 999999)

    await set_language_callback(update, FakeContext())

    assert query.last_answer[0] == "❌ User not found."


async def test_products_callback_lists_categories():
    make_category("Games")
    make_category("Gift Cards")
    query = FakeQuery(data="products", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await products_callback(update, FakeContext())

    assert "Select a Category" in query.last_edit_text


async def test_products_callback_empty_shows_no_categories_message():
    query = FakeQuery(data="products", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await products_callback(update, FakeContext())

    assert "No categories available" in query.last_edit_text


async def test_category_callback_with_subcategories():
    cat_id = make_category("Games")
    make_subcategory(cat_id, "Steam")
    query = FakeQuery(data=f"category_{cat_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await category_callback(update, FakeContext())

    assert "Games" in query.last_edit_text


async def test_category_callback_not_found():
    query = FakeQuery(data="category_999", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await category_callback(update, FakeContext())

    assert query.last_edit_text == "❌ Category not found."


async def test_show_products_list_for_category():
    cat_id = make_category("Games")
    make_product(category_id=cat_id, name="Steam Key", price="14.99", stock=3)
    query = FakeQuery(data=f"category_{cat_id}", user_id=TELEGRAM_ID)

    await show_products_list(query, category_id=cat_id, context=FakeContext())

    assert "Steam Key" not in (query.last_edit_text or "")  # products are buttons, not text
    assert "Select the product" in query.last_edit_text
    # The button with the product's price should be in the keyboard.
    keyboard = query.edits[-1][1]
    button_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("Steam Key" in text and "$14.99" in text for text in button_texts)


async def test_show_products_list_back_button_uses_parent_category():
    cat_id = make_category("Games")
    sub_id = make_subcategory(cat_id, "Steam")
    make_product(subcategory_id=sub_id, name="Steam Key")
    query = FakeQuery(data=f"subcategory_{sub_id}", user_id=TELEGRAM_ID)

    await show_products_list(query, subcategory_id=sub_id, context=FakeContext())

    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"category_{cat_id}" in callbacks


async def test_product_detail_callback_shows_details():
    product_id = make_product(name="Steam Key", price="14.99", stock=3)
    query = FakeQuery(data=f"product_{product_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await product_detail_callback(update, FakeContext())

    assert "Steam Key" in query.last_edit_text
    assert "$14.99" in query.last_edit_text


async def test_product_detail_callback_not_found():
    query = FakeQuery(data="product_999", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await product_detail_callback(update, FakeContext())

    assert query.last_edit_text == "❌ Product not found."


async def test_availability_callback_lists_products():
    cat_id = make_category("Games")
    make_product(category_id=cat_id, name="Steam Key", price="14.99", stock=3)
    query = FakeQuery(data="availability", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await availability_callback(update, FakeContext())

    assert "Steam Key" in query.last_edit_text


async def test_support_callback_shows_page():
    with get_db_session() as session:
        session.add(Settings(support_username="shopsupport", channel_username="shopchannel"))
    query = FakeQuery(data="support", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await support_callback(update, FakeContext())

    assert "My Shop is Open" in query.last_edit_text
    keyboard = query.edits[-1][1]
    urls = [btn.url for row in keyboard.inline_keyboard for btn in row if btn.url]
    assert any("shopsupport" in u for u in urls)


async def test_order_history_callback_lists_orders():
    user_id = make_user()
    make_order(user_id, total="19.99")
    query = FakeQuery(data="order_history", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await order_history_callback(update, FakeContext())

    keyboard = query.edits[-1][1]
    button_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("$19.99" in text for text in button_texts)


async def test_order_history_callback_no_orders():
    make_user()
    query = FakeQuery(data="order_history", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await order_history_callback(update, FakeContext())

    assert "No orders yet" in query.last_edit_text


async def test_user_order_detail_callback_shows_items_and_dispute_button():
    user_id = make_user()
    product_id = make_product(name="Steam Key", price="14.99")
    order_id = make_order(user_id, total="14.99", status=OrderStatus.COMPLETED, dispute=DisputeStatus.NIL)
    with get_db_session() as session:
        session.add(OrderItem(order_id=order_id, product_id=product_id, quantity=1, price=Decimal("14.99")))

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await user_order_detail_callback(update, FakeContext())

    assert "Steam Key" in query.last_edit_text
    assert f"Order #{order_id}" in query.last_edit_text
    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"open_dispute_{order_id}" in callbacks


async def test_user_order_detail_callback_order_not_found():
    make_user()
    query = FakeQuery(data="user_order_detail_999", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    await user_order_detail_callback(update, FakeContext())

    assert query.last_edit_text == "❌ Order not found."
