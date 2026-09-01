"""Admin handlers: refund idempotency (cancel_order) and the audit trail
(AdminActionLog) that's supposed to be written alongside every one of these.
"""

from decimal import Decimal

from database import (
    get_db_session, User, Product, ProductKey, Order, OrderStatus,
    ProductType, AdminActionLog,
)
from handlers.admin_handlers import admin_cancel_order_callback, admin_ban_user_callback
from fakes import FakeUpdate, FakeQuery, FakeContext

ADMIN_ID = 1000000001  # matches ADMIN_TELEGRAM_ID set in conftest.py
CUSTOMER_TELEGRAM_ID = 555003


def make_completed_order_with_keys(price="19.99", quantity=2) -> tuple[int, int, int]:
    """Returns (order_id, user_id, product_id) for a COMPLETED order that
    already took payment and assigned keys - the state admin_cancel_order
    is meant to unwind.
    """
    with get_db_session() as session:
        user = User(telegram_id=CUSTOMER_TELEGRAM_ID, wallet_balance=Decimal("0.00"))
        session.add(user)
        product = Product(
            name="Refund Test", price=Decimal(price), stock_count=0,
            product_type=ProductType.KEY, is_active=True,
        )
        session.add(product)
        session.flush()

        total = Decimal(price) * quantity
        order = Order(user_id=user.id, total_amount=total, status=OrderStatus.COMPLETED)
        session.add(order)
        session.flush()

        for i in range(quantity):
            session.add(ProductKey(
                product_id=product.id, key_value=f"SOLD-{i}",
                is_sold=True, order_id=order.id,
            ))

        return order.id, user.id, product.id


async def test_cancel_order_refunds_once_and_returns_keys():
    order_id, user_id, product_id = make_completed_order_with_keys(price="19.99", quantity=2)

    query = FakeQuery(data=f"cancel_order_{order_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    await admin_cancel_order_callback(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        order = session.query(Order).filter_by(id=order_id).first()
        product = session.query(Product).filter_by(id=product_id).first()
        unsold_keys = session.query(ProductKey).filter_by(product_id=product_id, is_sold=False).count()

        assert user.wallet_balance == Decimal("39.98")
        assert order.status == OrderStatus.CANCELLED
        assert product.stock_count == 2, "returned keys restore stock_count"
        assert unsold_keys == 2

        log = session.query(AdminActionLog).filter_by(action="cancel_order").first()
        assert log is not None
        assert log.admin_telegram_id == ADMIN_ID
        assert log.target_id == order_id


async def test_cancel_order_is_idempotent_on_repeated_clicks():
    """The bug this handler was rewritten to fix: clicking Cancel five times
    used to refund the order total five times.
    """
    order_id, user_id, _ = make_completed_order_with_keys(price="10.00", quantity=1)

    for _ in range(3):
        query = FakeQuery(data=f"cancel_order_{order_id}", user_id=ADMIN_ID)
        update = FakeUpdate(query, ADMIN_ID)
        context = FakeContext()
        await admin_cancel_order_callback(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("10.00"), "only the first click should have refunded"

        # Only one cancel_order log entry, even though the handler ran 3 times.
        logs = session.query(AdminActionLog).filter_by(action="cancel_order").all()
        assert len(logs) == 1


async def test_non_admin_cannot_cancel_order():
    order_id, user_id, _ = make_completed_order_with_keys(price="10.00", quantity=1)
    not_an_admin = 999999999

    query = FakeQuery(data=f"cancel_order_{order_id}", user_id=not_an_admin)
    update = FakeUpdate(query, not_an_admin)
    context = FakeContext()

    await admin_cancel_order_callback(update, context)

    assert query.last_answer[0] == "⛔ Access denied."
    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        assert user.wallet_balance == Decimal("0.00")
        assert session.query(AdminActionLog).count() == 0


async def test_ban_user_writes_audit_log():
    with get_db_session() as session:
        user = User(telegram_id=CUSTOMER_TELEGRAM_ID, wallet_balance=Decimal("0.00"))
        session.add(user)
        session.flush()
        user_id = user.id

    query = FakeQuery(data=f"ban_user_{user_id}", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    await admin_ban_user_callback(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        assert user.is_banned is True

        log = session.query(AdminActionLog).filter_by(action="ban_user").first()
        assert log is not None
        assert log.admin_telegram_id == ADMIN_ID
        assert log.target_id == user_id


async def test_is_admin_recognizes_extra_configured_admins(monkeypatch):
    from config.settings import settings
    from utils import is_admin

    extra_admin_id = 42424242
    monkeypatch.setattr(settings, "ADMIN_IDS", {ADMIN_ID, extra_admin_id})

    assert is_admin(ADMIN_ID)
    assert is_admin(extra_admin_id)
    assert not is_admin(1)
