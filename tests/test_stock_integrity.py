"""Stock count must stay equal to the keys actually available.

Three admin paths write stock_count on a product row that confirm_purchase
locks while it sells: restock recomputes it from a count of unsold keys,
cancelling an order adds one back per returned key, and clearing keys
writes zero. All three read-modify-write, and none held the lock. Lose the
race and the catalogue offers keys that have already been delivered - the
buyer finds out at checkout.
"""

from decimal import Decimal

import pytest

import database.db as db
from database.models import (
    Order, OrderItem, OrderStatus, Product, ProductKey, ProductType, User,
)
from fakes import FakeContext, FakeQuery, FakeUpdate
from handlers import admin_handlers

ADMIN_ID = 1000000001


def unsold(product_id) -> int:
    with db.get_db_session() as session:
        return session.query(ProductKey).filter_by(
            product_id=product_id, is_sold=False).count()


def advertised(product_id) -> int:
    with db.get_db_session() as session:
        return session.query(Product.stock_count).filter_by(
            id=product_id).scalar()


@pytest.fixture
def product(clean_db):
    """A key product with ten keys in stock."""
    with db.get_db_session() as session:
        item = Product(name="Licence", price=Decimal("5.00"),
                       product_type=ProductType.KEY, stock_count=10,
                       is_active=True)
        session.add(item)
        session.flush()
        for i in range(10):
            session.add(ProductKey(product_id=item.id, key_value=f"KEY-{i}",
                                   is_sold=False))
        session.commit()
        return item.id


def test_restocking_leaves_stock_equal_to_the_real_key_count(product):
    result = admin_handlers._restock_keys_sync(
        product, [f"NEW-{i}" for i in range(5)], ADMIN_ID)

    _name, added, skipped, new_stock = result
    assert (added, skipped) == (5, 0)
    assert new_stock == unsold(product) == advertised(product) == 15


def test_restocking_the_same_file_twice_adds_nothing(product):
    keys = [f"NEW-{i}" for i in range(5)]
    admin_handlers._restock_keys_sync(product, keys, ADMIN_ID)
    _name, added, skipped, new_stock = admin_handlers._restock_keys_sync(
        product, keys, ADMIN_ID)

    assert (added, skipped) == (0, 5)
    assert new_stock == unsold(product) == 15


def test_restocking_after_a_sale_does_not_resurrect_the_sold_keys(product):
    """The race in miniature: keys sell, then a restock recomputes."""
    with db.get_db_session() as session:
        for key in session.query(ProductKey).filter_by(
                product_id=product, is_sold=False).limit(4).all():
            key.is_sold = True
        session.query(Product).filter_by(id=product).first().stock_count = 6
        session.commit()

    _name, _added, _skipped, new_stock = admin_handlers._restock_keys_sync(
        product, ["EXTRA-1"], ADMIN_ID)

    assert new_stock == unsold(product) == advertised(product) == 7


async def test_cancelling_an_order_returns_every_key_once(product):
    """Two keys back means stock rises by two, not by one and not by four."""
    with db.get_db_session() as session:
        buyer = User(telegram_id=7777, wallet_balance=Decimal("0"))
        session.add(buyer)
        session.flush()
        order = Order(user_id=buyer.id, total_amount=Decimal("10.00"),
                      status=OrderStatus.COMPLETED)
        session.add(order)
        session.flush()
        session.add(OrderItem(order_id=order.id, product_id=product,
                              quantity=2, price=Decimal("5.00")))
        sold = session.query(ProductKey).filter_by(
            product_id=product, is_sold=False).limit(2).all()
        for key in sold:
            key.is_sold = True
            key.order_id = order.id
        session.query(Product).filter_by(id=product).first().stock_count = 8
        session.commit()
        order_id = order.id

    assert advertised(product) == 8

    update = FakeUpdate(FakeQuery(f"cancel_order_{order_id}", ADMIN_ID),
                        user_id=ADMIN_ID)
    await admin_handlers.admin_cancel_order_callback(update, FakeContext())

    assert unsold(product) == 10
    assert advertised(product) == 10


async def test_cancelling_an_order_refunds_the_buyer(product):
    """The keys coming back must not cost the refund."""
    with db.get_db_session() as session:
        buyer = User(telegram_id=7778, wallet_balance=Decimal("0.00"))
        session.add(buyer)
        session.flush()
        order = Order(user_id=buyer.id, total_amount=Decimal("10.00"),
                      status=OrderStatus.COMPLETED)
        session.add(order)
        session.flush()
        session.add(OrderItem(order_id=order.id, product_id=product,
                              quantity=2, price=Decimal("5.00")))
        for key in session.query(ProductKey).filter_by(
                product_id=product, is_sold=False).limit(2).all():
            key.is_sold = True
            key.order_id = order.id
        session.commit()
        order_id, buyer_id = order.id, buyer.id

    update = FakeUpdate(FakeQuery(f"cancel_order_{order_id}", ADMIN_ID),
                        user_id=ADMIN_ID)
    await admin_handlers.admin_cancel_order_callback(update, FakeContext())

    with db.get_db_session() as session:
        assert session.query(User.wallet_balance).filter_by(
            id=buyer_id).scalar() == Decimal("10.00")
        assert session.query(Order.status).filter_by(
            id=order_id).scalar() == OrderStatus.CANCELLED
