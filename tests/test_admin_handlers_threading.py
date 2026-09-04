"""handlers.admin_handlers - regression tests for the asyncio.to_thread
refactor, covering the handlers not already exercised by
tests/test_admin_actions.py (cancel_order, ban_user) or
tests/test_restock.py (restock). Focuses on the multi-branch sentinel
logic: admin_reactivate_order_callback, admin_complete_order_callback,
admin_confirm_payment_callback, admin_cancel_payment_callback, and the
paginated list views.
"""

from decimal import Decimal

from database import get_db_session, User, Order, OrderStatus, Transaction, TransactionStatus, PaymentMethod
from handlers.admin_handlers import (
    admin_reactivate_order_callback, admin_complete_order_callback,
    admin_confirm_payment_callback, admin_cancel_payment_callback,
    admin_view_orders_callback, admin_view_users_callback,
    admin_user_detail_callback,
)
from fakes import FakeUpdate, FakeQuery, FakeContext

ADMIN_ID = 1000000001  # matches conftest.py's ADMIN_TELEGRAM_ID
USER_TELEGRAM_ID = 777777


def make_user(balance="50.00") -> int:
    with get_db_session() as session:
        user = User(telegram_id=USER_TELEGRAM_ID, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def make_order(user_id, total="19.99", status=OrderStatus.CANCELLED) -> int:
    with get_db_session() as session:
        order = Order(user_id=user_id, total_amount=Decimal(total), status=status)
        session.add(order)
        session.flush()
        return order.id


def make_transaction(user_id, amount="10.00", status=TransactionStatus.PENDING) -> int:
    with get_db_session() as session:
        txn = Transaction(user_id=user_id, amount=Decimal(amount), payment_method=PaymentMethod.CARD, status=status)
        session.add(txn)
        session.flush()
        return txn.id


async def test_reactivate_order_debits_refund_and_reopens():
    user_id = make_user(balance="50.00")
    order_id = make_order(user_id, total="20.00", status=OrderStatus.CANCELLED)
    query = FakeQuery(data=f"reactivate_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_reactivate_order_callback(update, FakeContext())

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()
        assert order.status == OrderStatus.PROCESSING
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("30.00")

    assert query.last_answer[0] == "✅ Order reactivated."


async def test_reactivate_order_rejects_insufficient_wallet():
    user_id = make_user(balance="5.00")
    order_id = make_order(user_id, total="20.00", status=OrderStatus.CANCELLED)
    query = FakeQuery(data=f"reactivate_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_reactivate_order_callback(update, FakeContext())

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()
        assert order.status == OrderStatus.CANCELLED  # unchanged

    assert "no longer available" in query.last_answer[0]


async def test_reactivate_order_rejects_non_cancelled_order():
    user_id = make_user()
    order_id = make_order(user_id, status=OrderStatus.PROCESSING)
    query = FakeQuery(data=f"reactivate_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_reactivate_order_callback(update, FakeContext())

    assert "Only cancelled orders" in query.last_answer[0]


async def test_complete_order_marks_completed():
    user_id = make_user()
    order_id = make_order(user_id, status=OrderStatus.PROCESSING)
    query = FakeQuery(data=f"complete_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_complete_order_callback(update, FakeContext())

    with get_db_session() as session:
        order = session.query(Order).filter_by(id=order_id).first()
        assert order.status == OrderStatus.COMPLETED
        assert order.completed_at is not None


async def test_complete_order_already_completed_is_idempotent():
    user_id = make_user()
    order_id = make_order(user_id, status=OrderStatus.COMPLETED)
    query = FakeQuery(data=f"complete_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_complete_order_callback(update, FakeContext())

    assert query.last_edit_text == "ℹ️ Order is already completed."


async def test_confirm_payment_credits_wallet_and_notifies_user():
    user_id = make_user(balance="0.00")
    txn_id = make_transaction(user_id, amount="25.00")
    query = FakeQuery(data=f"confirm_payment_{txn_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    await admin_confirm_payment_callback(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("25.00")
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        assert txn.status == TransactionStatus.COMPLETED

    assert context.bot.sent  # user notified
    assert "confirmed" in query.last_answer[0].lower()


async def test_confirm_payment_rejects_already_processed():
    user_id = make_user(balance="0.00")
    txn_id = make_transaction(user_id, status=TransactionStatus.COMPLETED)
    query = FakeQuery(data=f"confirm_payment_{txn_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_confirm_payment_callback(update, FakeContext())

    assert "already" in query.last_answer[0].lower()

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("0.00")  # not double-credited


async def test_cancel_payment_marks_failed_and_notifies_user():
    user_id = make_user(balance="0.00")
    txn_id = make_transaction(user_id, amount="15.00")
    query = FakeQuery(data=f"cancel_payment_{txn_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    await admin_cancel_payment_callback(update, context)

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        assert txn.status == TransactionStatus.FAILED
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("0.00")  # never credited

    assert context.bot.sent


async def test_admin_view_orders_lists_orders():
    user_id = make_user()
    make_order(user_id, total="42.50", status=OrderStatus.PROCESSING)
    query = FakeQuery(data="admin_view_orders", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_view_orders_callback(update, FakeContext())

    keyboard = query.edits[-1][1]
    button_texts = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("$42.50" in text for text in button_texts)


async def test_admin_view_users_lists_users():
    make_user()
    query = FakeQuery(data="admin_view_users", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_view_users_callback(update, FakeContext())

    assert "Select a user" in query.last_edit_text


async def test_admin_user_detail_shows_ban_button_for_active_user():
    user_id = make_user()
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        internal_id = user.id
    query = FakeQuery(data=f"view_user_{internal_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_user_detail_callback(update, FakeContext())

    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"ban_user_{internal_id}" in callbacks
