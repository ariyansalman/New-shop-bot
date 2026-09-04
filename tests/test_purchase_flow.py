"""handlers.payment_handlers.confirm_purchase - the highest-risk path in the
bot (it's the one that moves money and hands out product keys).

Driven through the real handler function with fake Telegram objects, against
a real (SQLite) database - this is closer to an integration test than a unit
test, deliberately: the bug this handler was originally written to fix
(a committed COMPLETED order with no keys and no wallet debit on a stock
shortage) only shows up when the whole transaction is exercised together.

pytest-asyncio's asyncio_mode = "auto" (see pyproject.toml) runs these
`async def` tests directly - no event-loop boilerplate needed.
"""

from decimal import Decimal

from database import get_db_session, User, Product, ProductKey, Order, ProductType
from handlers.payment_handlers import confirm_purchase, MAX_PURCHASE_QUANTITY
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 555001


def make_user(balance="100.00") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def make_key_product(price="19.99", key_count=3) -> int:
    with get_db_session() as session:
        product = Product(
            name="Test Key", price=Decimal(price), stock_count=key_count,
            product_type=ProductType.KEY, is_active=True,
        )
        session.add(product)
        session.flush()
        for i in range(key_count):
            session.add(ProductKey(product_id=product.id, key_value=f"KEY-{i}", is_sold=False))
        return product.id


async def test_successful_purchase_debits_wallet_and_assigns_keys():
    make_user(balance="100.00")
    product_id = make_key_product(price="19.99", key_count=3)

    query = FakeQuery(data=f"confirm_purchase_{product_id}_2", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    await confirm_purchase(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        product = session.query(Product).filter_by(id=product_id).first()
        orders = session.query(Order).filter_by(user_id=user.id).all()
        sold_keys = session.query(ProductKey).filter_by(product_id=product_id, is_sold=True).all()

        assert user.wallet_balance == Decimal("60.02"), "100.00 - 2*19.99"
        assert product.stock_count == 1
        assert len(orders) == 1
        assert len(sold_keys) == 2

    assert "Purchase Successful" in query.last_edit_text
    assert len(context.bot.sent) == 1  # admin notification


async def test_insufficient_balance_charges_nothing():
    make_user(balance="10.00")
    product_id = make_key_product(price="19.99", key_count=3)

    query = FakeQuery(data=f"confirm_purchase_{product_id}_1", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    await confirm_purchase(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        product = session.query(Product).filter_by(id=product_id).first()
        assert user.wallet_balance == Decimal("10.00"), "must not be charged"
        assert product.stock_count == 3, "no keys should be touched"
        assert session.query(Order).count() == 0

    assert "Insufficient balance" in query.last_edit_text


async def test_key_shortage_charges_nothing_and_repairs_stock_count():
    """The advertised stock_count lies (says 5, only 2 keys actually exist) -
    the bug this handler was rewritten to fix: it must bail out BEFORE
    charging anyone, and correct the advertised count while it's at it.
    """
    make_user(balance="1000.00")
    product_id = make_key_product(price="10.00", key_count=2)
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()
        product.stock_count = 5  # lie: only 2 keys actually exist

    query = FakeQuery(data=f"confirm_purchase_{product_id}_5", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    await confirm_purchase(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        product = session.query(Product).filter_by(id=product_id).first()
        assert user.wallet_balance == Decimal("1000.00"), "must not be charged"
        assert product.stock_count == 2, "advertised stock repaired to reality"
        assert session.query(Order).count() == 0

    assert "Not enough keys" in query.last_edit_text


async def test_quantity_zero_or_over_ceiling_rejected_before_touching_db():
    make_user(balance="1000.00")
    product_id = make_key_product(price="10.00", key_count=10)

    for bad_quantity in (0, MAX_PURCHASE_QUANTITY + 1):
        query = FakeQuery(data=f"confirm_purchase_{product_id}_{bad_quantity}", user_id=TELEGRAM_ID)
        update = FakeUpdate(query, TELEGRAM_ID)
        context = FakeContext()

        await confirm_purchase(update, context)

        assert "must be between" in query.last_edit_text

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        assert user.wallet_balance == Decimal("1000.00")
        assert session.query(Order).count() == 0


async def test_file_product_purchase_delivers_download_link():
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal("50.00"))
        session.add(user)
        product = Product(
            name="Test File", price=Decimal("15.00"), stock_count=999,
            product_type=ProductType.FILE, is_active=True,
            download_link="https://example.com/file.zip",
        )
        session.add(product)
        session.flush()
        product_id = product.id

    query = FakeQuery(data=f"confirm_purchase_{product_id}_1", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    await confirm_purchase(update, context)

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=TELEGRAM_ID).first()
        assert user.wallet_balance == Decimal("35.00")

    assert "example.com/file.zip" in query.last_edit_text
