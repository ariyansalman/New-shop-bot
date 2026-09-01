"""handlers.dispute_handlers - regression tests for the asyncio.to_thread
refactor. Driven through the real handler functions against a real
(SQLite) database, like the other *_threading.py test files.
"""

from decimal import Decimal

from database import get_db_session, User, Order, Dispute, OrderStatus, DisputeStatus
from handlers.dispute_handlers import (
    open_dispute_start, admin_view_disputes_callback,
    admin_dispute_detail_callback, admin_resolve_dispute_callback,
)
from telegram.ext import ConversationHandler
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 999001
ADMIN_ID = 1000000001  # matches conftest.py's ADMIN_TELEGRAM_ID


def make_user(telegram_id=TELEGRAM_ID) -> int:
    with get_db_session() as session:
        user = User(telegram_id=telegram_id, wallet_balance=Decimal("0"))
        session.add(user)
        session.flush()
        return user.id


def make_order(user_id, total="19.99", dispute=DisputeStatus.NIL) -> int:
    with get_db_session() as session:
        order = Order(user_id=user_id, total_amount=Decimal(total), status=OrderStatus.COMPLETED, dispute_status=dispute)
        session.add(order)
        session.flush()
        return order.id


def make_dispute(order_id, user_id, reason="Item not received", status=DisputeStatus.OPENED) -> int:
    with get_db_session() as session:
        dispute = Dispute(order_id=order_id, user_id=user_id, reason=reason, status=status)
        session.add(dispute)
        session.flush()
        return dispute.id


async def test_open_dispute_start_success_prompts_for_reason():
    user_id = make_user()
    order_id = make_order(user_id)
    query = FakeQuery(data=f"open_dispute_{order_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    result = await open_dispute_start(update, context)

    assert f"Order #{order_id}" in query.last_edit_text
    assert context.user_data['dispute_order_id'] == order_id
    assert result != ConversationHandler.END


async def test_open_dispute_start_already_disputed():
    user_id = make_user()
    order_id = make_order(user_id, dispute=DisputeStatus.OPENED)
    query = FakeQuery(data=f"open_dispute_{order_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    result = await open_dispute_start(update, FakeContext())

    assert "already has a dispute" in query.last_edit_text
    assert result == ConversationHandler.END


async def test_open_dispute_start_order_not_found():
    make_user()
    query = FakeQuery(data="open_dispute_999", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)

    result = await open_dispute_start(update, FakeContext())

    assert query.last_edit_text == "❌ Order not found."
    assert result == ConversationHandler.END


async def test_admin_view_disputes_lists_open_disputes():
    user_id = make_user()
    order_id = make_order(user_id)
    make_dispute(order_id, user_id)
    query = FakeQuery(data="admin_view_disputes", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_view_disputes_callback(update, FakeContext())

    assert "Open Disputes (1)" in query.last_edit_text


async def test_admin_view_disputes_empty():
    query = FakeQuery(data="admin_view_disputes", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_view_disputes_callback(update, FakeContext())

    assert "No open disputes" in query.last_edit_text


async def test_admin_dispute_detail_shows_reason():
    user_id = make_user()
    order_id = make_order(user_id)
    dispute_id = make_dispute(order_id, user_id, reason="Wrong item delivered")
    query = FakeQuery(data=f"admin_dispute_detail_{dispute_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_dispute_detail_callback(update, FakeContext())

    assert "Wrong item delivered" in query.last_edit_text
    keyboard = query.edits[-1][1]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"resolve_dispute_{dispute_id}" in callbacks


async def test_admin_resolve_dispute_marks_resolved_and_notifies_user():
    user_id = make_user()
    order_id = make_order(user_id)
    dispute_id = make_dispute(order_id, user_id)
    query = FakeQuery(data=f"resolve_dispute_{dispute_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    await admin_resolve_dispute_callback(update, context)

    with get_db_session() as session:
        dispute = session.query(Dispute).filter_by(id=dispute_id).first()
        assert dispute.status == DisputeStatus.RESOLVED
        order = session.query(Order).filter_by(id=order_id).first()
        assert order.dispute_status == DisputeStatus.RESOLVED

    assert context.bot.sent  # user was notified
    assert "resolved" in query.last_edit_text.lower()


async def test_admin_resolve_dispute_already_resolved_is_idempotent():
    user_id = make_user()
    order_id = make_order(user_id)
    dispute_id = make_dispute(order_id, user_id, status=DisputeStatus.RESOLVED)
    query = FakeQuery(data=f"resolve_dispute_{dispute_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)

    await admin_resolve_dispute_callback(update, FakeContext())

    assert "already resolved" in query.last_edit_text.lower()
