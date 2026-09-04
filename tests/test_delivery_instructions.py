"""Per-product delivery instructions, from admin input to the receipt.

The part worth pinning is the snapshot: the order line keeps its own copy
taken at purchase time. Reading live from the product instead would show a
customer instructions that were edited after they bought, and would show
them nothing at all once the product is deleted - which admins can do, and
which the order line is explicitly built to survive.
"""

from decimal import Decimal

from database import (
    get_db_session, User, Product, ProductKey, ProductType,
    Order, OrderItem, OrderStatus,
)
from handlers import user_handlers as uh
from handlers.admin_conversations import products as ap
from fakes import FakeUpdate, FakeQuery, FakeContext, FakeMessage, FakeMessageUpdate

TELEGRAM_ID = 909004
INSTRUCTIONS = "1. Go to netflix.com/redeem\n2. Enter the code above"


def make_product(instructions=INSTRUCTIONS, keys=("KEY-1",)):
    with get_db_session() as session:
        product = Product(
            name="Netflix 1 Month", description="d", price=Decimal("10.00"),
            product_type=ProductType.KEY, stock_count=len(keys),
            delivery_instructions=instructions, is_active=True,
        )
        session.add(product)
        session.flush()
        for value in keys:
            session.add(ProductKey(product_id=product.id, key_value=value))
        return product.id


def make_order(product_id, instructions=INSTRUCTIONS, delivered="KEY-1"):
    """An order line as the purchase flow leaves it - instructions copied."""
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal("0.00"))
        session.add(user)
        session.flush()
        order = Order(user_id=user.id, total_amount=Decimal("10.00"),
                      status=OrderStatus.COMPLETED)
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, product_id=product_id, quantity=1,
            price=Decimal("10.00"), delivered_asset=delivered,
            delivery_instructions=instructions,
        ))
        return order.id


# ---------------------------------------------------------------- model

def test_instructions_are_optional():
    product_id = make_product(instructions=None)
    with get_db_session() as session:
        assert session.get(Product, product_id).delivery_instructions is None


# ------------------------------------------------------- order history

async def test_order_history_shows_the_instructions():
    product_id = make_product()
    order_id = make_order(product_id)

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=TELEGRAM_ID)
    await uh.user_order_detail_callback(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "How to use it" in query.last_edit_text
    assert "netflix.com/redeem" in query.last_edit_text


async def test_history_shows_what_was_sold_not_the_edited_product():
    """Editing a product must not rewrite an existing customer's receipt."""
    product_id = make_product()
    order_id = make_order(product_id)

    with get_db_session() as session:
        session.get(Product, product_id).delivery_instructions = "TOTALLY NEW TEXT"

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=TELEGRAM_ID)
    await uh.user_order_detail_callback(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "netflix.com/redeem" in query.last_edit_text
    assert "TOTALLY NEW TEXT" not in query.last_edit_text


async def test_history_survives_the_product_being_deleted():
    product_id = make_product()
    order_id = make_order(product_id)

    with get_db_session() as session:
        # Admins can do this; order_items.product_id is nullable for it.
        session.query(OrderItem).filter_by(product_id=product_id).update(
            {"product_id": None})
        session.query(ProductKey).filter_by(product_id=product_id).delete()
        session.query(Product).filter_by(id=product_id).delete()

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=TELEGRAM_ID)
    await uh.user_order_detail_callback(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "netflix.com/redeem" in query.last_edit_text


async def test_no_instructions_adds_nothing_to_the_receipt():
    product_id = make_product(instructions=None)
    order_id = make_order(product_id, instructions=None)

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=TELEGRAM_ID)
    await uh.user_order_detail_callback(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "How to use it" not in query.last_edit_text
    assert "KEY-1" in query.last_edit_text      # delivery itself is unchanged


# ------------------------------------------------- admin creation step

async def test_skip_leaves_the_product_without_instructions():
    context = FakeContext()
    context.user_data['product_type'] = ProductType.KEY
    update = FakeMessageUpdate(FakeMessage(text="skip"), user_id=1)

    state = await ap.product_instructions(update, context)

    assert context.user_data['product_instructions'] is None
    assert state == ap.PRODUCT_KEYS


async def test_instructions_are_stored_and_the_flow_moves_on():
    context = FakeContext()
    context.user_data['product_type'] = ProductType.FILE
    update = FakeMessageUpdate(FakeMessage(text=INSTRUCTIONS), user_id=1)

    state = await ap.product_instructions(update, context)

    assert context.user_data['product_instructions'] == INSTRUCTIONS
    assert state == ap.PRODUCT_DOWNLOAD_LINK


async def test_overlong_instructions_are_refused():
    """Telegram rejects a message over 4096 chars - that would be a paid
    order whose keys never arrive."""
    context = FakeContext()
    context.user_data['product_type'] = ProductType.KEY
    update = FakeMessageUpdate(FakeMessage(text="x" * 5000), user_id=1)

    state = await ap.product_instructions(update, context)

    assert state == ap.PRODUCT_INSTRUCTIONS         # asked again
    assert 'product_instructions' not in context.user_data
    assert "under" in update.message.replies[-1][0]
