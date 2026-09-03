"""Telegram rejects a text message over 4096 characters.

The delivery message for an order is built by listing every key it
contains, and the purchase ceiling is 1000. At roughly 180 keys the
message crosses the limit - and by the time it is sent the wallet has
been debited and the keys marked sold. A refused send there means the
customer has paid for keys they can never see, in the delivery message or
in their order history, which is built the same way.
"""

from decimal import Decimal

from database import (
    get_db_session, User, Product, ProductKey, ProductType, Order,
)
from handlers import payment_handlers as ph
from handlers import user_handlers as uh
from utils import split_message, MAX_MESSAGE
from fakes import FakeUpdate, FakeQuery, FakeContext

BUYER = 5001


def make_bulk_product(keys=300):
    with get_db_session() as session:
        session.add(User(telegram_id=BUYER, wallet_balance=Decimal("10000")))
        product = Product(name="Bulk Key", price=Decimal("1.00"),
                          stock_count=keys, product_type=ProductType.KEY,
                          is_active=True)
        session.add(product)
        session.flush()
        for index in range(keys):
            session.add(ProductKey(product_id=product.id,
                                   key_value=f"XXXX-YYYY-ZZZZ-{index:06d}"))
        return product.id


# ------------------------------------------------------------ splitting

def test_a_short_message_is_left_alone():
    assert split_message("hello") == ["hello"]


def test_every_part_fits():
    text = "\n".join(f"KEY-{i:06d}-XXXX-YYYY" for i in range(400))

    parts = split_message(text)

    assert len(parts) > 1
    assert all(len(part) <= MAX_MESSAGE for part in parts)


def test_splitting_loses_nothing():
    text = "\n".join(f"KEY-{i:06d}-XXXX-YYYY" for i in range(400))

    assert "\n".join(split_message(text)) == text


def test_a_key_is_never_broken_in_half():
    """Half a licence key is worse than a second message."""
    text = "\n".join(f"KEY-{i:06d}-XXXX-YYYY" for i in range(400))

    for part in split_message(text):
        for line in part.split("\n"):
            assert line == "" or line.startswith("KEY-"), line
            assert line == "" or len(line) == len("KEY-000000-XXXX-YYYY")


def test_one_enormous_line_is_hard_split_rather_than_dropped():
    parts = split_message("x" * 9000)

    assert all(len(part) <= MAX_MESSAGE for part in parts)
    assert "".join(parts) == "x" * 9000


# ------------------------------------------------------------- delivery

async def test_a_bulk_purchase_delivers_every_key():
    """The regression: 300 keys built a 6,700-character message that
    Telegram refused, after the wallet was already debited."""
    product_id = make_bulk_product(300)

    query = FakeQuery(data=f"confirm_purchase_{product_id}_300", user_id=BUYER)
    await ph.confirm_purchase(FakeUpdate(query, BUYER), FakeContext())

    parts = [text for text, _markup in query.edits]
    assert all(len(part) <= MAX_MESSAGE for part in parts)

    delivered = "\n".join(parts)
    missing = [i for i in range(300) if f"ZZZZ-{i:06d}" not in delivered]
    assert not missing, f"{len(missing)} keys never reached the customer"


async def test_the_keyboard_lands_on_the_last_part():
    """Otherwise the buttons scroll away above the keys."""
    product_id = make_bulk_product(300)

    query = FakeQuery(data=f"confirm_purchase_{product_id}_300", user_id=BUYER)
    await ph.confirm_purchase(FakeUpdate(query, BUYER), FakeContext())

    assert query.edits[-1][1] is not None
    assert all(markup is None for _t, markup in query.edits[:-1])


async def test_order_history_shows_every_key_too():
    """The fallback the customer is told to use has to work as well."""
    product_id = make_bulk_product(300)
    query = FakeQuery(data=f"confirm_purchase_{product_id}_300", user_id=BUYER)
    await ph.confirm_purchase(FakeUpdate(query, BUYER), FakeContext())

    with get_db_session() as session:
        order_id = session.query(Order.id).scalar()

    query = FakeQuery(data=f"user_order_detail_{order_id}", user_id=BUYER)
    await uh.user_order_detail_callback(FakeUpdate(query, BUYER), FakeContext())

    parts = [text for text, _markup in query.edits]
    assert all(len(part) <= MAX_MESSAGE for part in parts)

    shown = "\n".join(parts)
    missing = [i for i in range(300) if f"ZZZZ-{i:06d}" not in shown]
    assert not missing, f"{len(missing)} keys unreachable in order history"


async def test_a_normal_order_is_still_one_message():
    """Splitting must not change the common case."""
    product_id = make_bulk_product(3)

    query = FakeQuery(data=f"confirm_purchase_{product_id}_3", user_id=BUYER)
    await ph.confirm_purchase(FakeUpdate(query, BUYER), FakeContext())

    assert len(query.edits) == 1
