"""handlers.payment_handlers - regression tests for the asyncio.to_thread
refactor of the per-update handlers (buy_product_start,
show_purchase_confirmation, cancel_purchase, precheckout_callback).

confirm_purchase itself already has thorough coverage in
tests/test_purchase_flow.py; this file covers the handlers around it that
picked up the same _sync()-returns-plain-data pattern, to catch a
mismatched sentinel or a stale ORM reference crossing the thread boundary.
"""

from decimal import Decimal

from database import get_db_session, User, Product, ProductType, Transaction, TransactionStatus, PaymentMethod
from handlers.payment_handlers import (
    buy_product_start, show_purchase_confirmation, cancel_purchase,
    precheckout_callback, PURCHASE_QUANTITY,
)
from telegram.ext import ConversationHandler
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 888001


def make_user(balance="100.00") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def make_key_product(price="19.99", stock=3, is_active=True) -> int:
    with get_db_session() as session:
        product = Product(
            name="Test Key", price=Decimal(price), stock_count=stock,
            product_type=ProductType.KEY, is_active=is_active,
        )
        session.add(product)
        session.flush()
        return product.id


class _FakePreCheckoutQuery:
    def __init__(self, invoice_payload):
        self.invoice_payload = invoice_payload
        self.answers = []

    async def answer(self, ok, error_message=None):
        self.answers.append((ok, error_message))


class _FakePreCheckoutUpdate:
    def __init__(self, query):
        self.pre_checkout_query = query


async def test_buy_product_start_not_found():
    make_user()
    query = FakeQuery(data="buy_999", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    result = await buy_product_start(update, FakeContext())

    assert query.last_edit_text == "❌ Product not found."
    assert result == ConversationHandler.END


async def test_buy_product_start_inactive():
    make_user()
    product_id = make_key_product(is_active=False)
    query = FakeQuery(data=f"buy_{product_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    result = await buy_product_start(update, FakeContext())

    assert query.last_edit_text == "❌ This product is no longer available."
    assert result == ConversationHandler.END


async def test_buy_product_start_out_of_stock():
    make_user()
    product_id = make_key_product(stock=0)
    query = FakeQuery(data=f"buy_{product_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    result = await buy_product_start(update, FakeContext())

    assert query.last_edit_text == "❌ This product is out of stock."
    assert result == ConversationHandler.END


async def test_buy_product_start_success_prompts_for_quantity():
    make_user()
    product_id = make_key_product(price="14.99", stock=5)
    query = FakeQuery(data=f"buy_{product_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    result = await buy_product_start(update, context)

    assert "Test Key" in query.last_edit_text
    assert "$14.99" in query.last_edit_text
    assert context.user_data['purchase_product_id'] == product_id
    assert context.user_data['purchase_product_stock'] == 5
    assert result == PURCHASE_QUANTITY


async def test_show_purchase_confirmation_sufficient_balance():
    make_user(balance="100.00")
    query = FakeQuery(data="noop", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()
    context.user_data.update({
        'purchase_product_id': 1,
        'purchase_product_name': "Test Key",
        'purchase_product_price': Decimal("14.99"),
        'purchase_quantity': 2,
    })

    result = await show_purchase_confirmation(update, context)

    assert "$29.98" in query.last_edit_text
    assert "$100.00" in query.last_edit_text
    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "confirm_purchase_1_2" in callbacks
    assert result == ConversationHandler.END


async def test_show_purchase_confirmation_insufficient_balance_offers_topup():
    make_user(balance="5.00")
    query = FakeQuery(data="noop", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()
    context.user_data.update({
        'purchase_product_id': 1,
        'purchase_product_name': "Test Key",
        'purchase_product_price': Decimal("14.99"),
        'purchase_quantity': 1,
    })

    await show_purchase_confirmation(update, context)

    assert "Insufficient" in query.last_edit_text
    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "topup" in callbacks
    assert "confirm_purchase_1_1" not in callbacks


async def test_show_purchase_confirmation_user_not_found():
    query = FakeQuery(data="noop", user_id=999999)
    update = FakeUpdate(query, 999999)
    context = FakeContext()
    context.user_data.update({
        'purchase_product_id': 1,
        'purchase_product_name': "Test Key",
        'purchase_product_price': Decimal("14.99"),
        'purchase_quantity': 1,
    })

    result = await show_purchase_confirmation(update, context)

    assert query.last_edit_text == "❌ User not found."
    assert result == ConversationHandler.END


async def test_cancel_purchase_clears_context_and_shows_menu():
    make_user()
    query = FakeQuery(data="cancel_purchase", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['purchase_product_id'] = 1

    result = await cancel_purchase(update, context)

    assert "cancelled" in query.last_edit_text.lower()
    assert 'purchase_product_id' not in context.user_data
    assert result == ConversationHandler.END


async def test_precheckout_callback_accepts_valid_pending_transaction():
    user_id = make_user()
    with get_db_session() as session:
        txn = Transaction(
            user_id=user_id, amount=Decimal("10.00"),
            payment_method=PaymentMethod.CARD, status=TransactionStatus.PENDING,
        )
        session.add(txn)
        session.flush()
        txn_id = txn.id

    query = _FakePreCheckoutQuery(invoice_payload=f"topup_{txn_id}")
    update = _FakePreCheckoutUpdate(query)

    await precheckout_callback(update, FakeContext())

    assert query.answers == [(True, None)]


async def test_precheckout_callback_rejects_already_completed_transaction():
    user_id = make_user()
    with get_db_session() as session:
        txn = Transaction(
            user_id=user_id, amount=Decimal("10.00"),
            payment_method=PaymentMethod.CARD, status=TransactionStatus.COMPLETED,
        )
        session.add(txn)
        session.flush()
        txn_id = txn.id

    query = _FakePreCheckoutQuery(invoice_payload=f"topup_{txn_id}")
    update = _FakePreCheckoutUpdate(query)

    await precheckout_callback(update, FakeContext())

    assert query.answers[0][0] is False
