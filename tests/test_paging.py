"""Reading one page of rows instead of the whole table.

Six admin list screens loaded their entire table, built an object per
row, and kept five - on every tap of Next. Measured on a store with two
hundred orders, one tap on the order list cost 201 SELECTs, because the
buyer's name was fetched with a separate query per order. It is 2 now.
"""

from decimal import Decimal

import pytest
from sqlalchemy import event

import database.db as db
from database.models import Order, OrderStatus, User
from fakes import FakeContext, FakeQuery, FakeUpdate
from handlers import admin_handlers
from utils.paging import PAGE_SIZE, Page, page_number, page_of

ADMIN_ID = 1000000001


# ------------------------------------------------------ page_number

@pytest.mark.parametrize("data,expected", [
    ("admin_view_users", 0),
    ("admin_view_users_page_0", 0),
    ("admin_view_users_page_3", 3),
    ("admin_edit_subcategory_page_12", 12),
])
def test_the_page_a_callback_asks_for(data, expected):
    assert page_number(data) == expected


@pytest.mark.parametrize("data", [
    None, "", "admin_view_users_page_", "admin_view_users_page_x",
    "admin_view_users_page_2_page_", "_page_",
])
def test_a_callback_that_does_not_match_the_shape_never_raises(data):
    """Six copies of a bare int() were six ways to make a dead button."""
    assert page_number(data) == 0


def test_a_negative_page_is_clamped_not_used_as_a_python_slice():
    """page=-1 would otherwise offset from the end of the table."""
    assert page_number("admin_view_users_page_-1") == 0


# ---------------------------------------------------------- page_of

@pytest.fixture
def orders(clean_db):
    """A store with enough orders to page through."""
    with db.get_db_session() as session:
        for i in range(12):
            user = User(telegram_id=2000 + i, username=f"buyer{i}",
                        wallet_balance=Decimal("0"))
            session.add(user)
            session.flush()
            session.add(Order(user_id=user.id, total_amount=Decimal("9.99"),
                              status=OrderStatus.COMPLETED))
        session.commit()


def test_a_page_holds_only_its_own_rows(orders):
    with db.get_db_session() as session:
        page = page_of(session.query(Order).order_by(Order.id), 0)
        assert len(page.rows) == PAGE_SIZE
        assert page.total == 12
        assert page.total_pages == 3


def test_the_last_page_holds_the_remainder(orders):
    with db.get_db_session() as session:
        page = page_of(session.query(Order).order_by(Order.id), 2)
        assert len(page.rows) == 2
        assert page.has_next is False
        assert page.has_previous is True


def test_paging_through_visits_every_row_exactly_once(orders):
    seen = []
    with db.get_db_session() as session:
        for number in range(3):
            page = page_of(session.query(Order).order_by(Order.id), number)
            seen += [order.id for order in page.rows]
    assert len(seen) == 12
    assert len(set(seen)) == 12


def test_a_page_past_the_end_lands_on_the_last_one(orders):
    """Rows get deleted while someone is paging; an empty screen reads as
    breakage, so the request is clamped instead."""
    with db.get_db_session() as session:
        page = page_of(session.query(Order).order_by(Order.id), 99)
    assert page.number == 2
    assert len(page.rows) == 2


def test_an_empty_table_is_one_empty_page(clean_db):
    with db.get_db_session() as session:
        page = page_of(session.query(Order).order_by(Order.id), 0)
    assert page.rows == []
    assert page.total == 0
    assert page.total_pages == 1
    assert page.has_next is False


def test_the_label_reads_from_one_not_zero():
    assert Page(rows=[], number=0, total_pages=3, total=12).label == "Page 1/3"


# ------------------------------------------------- the actual screens

async def test_one_tap_on_the_order_list_is_a_constant_number_of_queries(orders):
    """The regression this guards: a User query per order, plus the table."""
    statements = []

    def record(conn, cursor, statement, *args):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        update = FakeUpdate(FakeQuery("admin_view_orders", ADMIN_ID), user_id=ADMIN_ID)
        await admin_handlers.admin_view_orders_callback(update, FakeContext())
    finally:
        event.remove(db.engine, "before_cursor_execute", record)

    # One count, one page. Twelve orders must not cost twelve more.
    assert statements, "the screen ran no query at all - is the admin check passing?"
    assert len(statements) <= 3, f"{len(statements)} SELECTs for one screen"


async def test_the_order_list_still_shows_the_buyer(orders):
    update = FakeUpdate(FakeQuery("admin_view_orders", ADMIN_ID), user_id=ADMIN_ID)
    await admin_handlers.admin_view_orders_callback(update, FakeContext())

    text, markup = update.callback_query.edits[-1]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("buyer" in label for label in labels)
    assert "Page 1/3" in text


async def test_the_order_list_offers_a_next_page(orders):
    update = FakeUpdate(FakeQuery("admin_view_orders", ADMIN_ID), user_id=ADMIN_ID)
    await admin_handlers.admin_view_orders_callback(update, FakeContext())

    _text, markup = update.callback_query.edits[-1]
    targets = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "admin_view_orders_page_1" in targets


async def test_the_second_page_shows_different_orders(orders):
    first = FakeUpdate(FakeQuery("admin_view_orders", ADMIN_ID), user_id=ADMIN_ID)
    await admin_handlers.admin_view_orders_callback(first, FakeContext())
    second = FakeUpdate(FakeQuery("admin_view_orders_page_1", ADMIN_ID), user_id=ADMIN_ID)
    await admin_handlers.admin_view_orders_callback(second, FakeContext())

    def order_targets(update):
        _text, markup = update.callback_query.edits[-1]
        return {b.callback_data for row in markup.inline_keyboard for b in row
                if b.callback_data.startswith("view_order_")}

    assert order_targets(first)
    assert not order_targets(first) & order_targets(second)


async def test_the_user_list_pages_without_loading_every_user(clean_db):
    with db.get_db_session() as session:
        for i in range(9):
            session.add(User(telegram_id=3000 + i, username=f"u{i}",
                             wallet_balance=Decimal("0")))
        session.commit()

    update = FakeUpdate(FakeQuery("admin_view_users", ADMIN_ID), user_id=ADMIN_ID)
    await admin_handlers.admin_view_users_callback(update, FakeContext())

    _text, markup = update.callback_query.edits[-1]
    rows = [b for row in markup.inline_keyboard for b in row
            if b.callback_data.startswith("view_user_")]
    assert len(rows) == PAGE_SIZE
