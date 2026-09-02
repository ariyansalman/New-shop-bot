"""The Wallet screen and the store's Terms & FAQ.

Wallet exists to close one gap: Order History shows orders, not payments,
so a customer who had paid and was waiting on verification had nowhere to
look. With Binance a top-up can sit in VERIFYING or MANUAL_REVIEW for
minutes, and every one of those was a support message.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from database import (
    get_db_session, User, Transaction, TransactionStatus, PaymentMethod,
    Settings as StoreSettings, AdminActionLog,
)
from services import store_content
from handlers import user_handlers as uh
from utils import create_main_menu_keyboard
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 909005


@pytest.fixture(autouse=True)
def no_terms(monkeypatch):
    monkeypatch.setattr(store_content, "_has_terms", False, raising=False)
    # check_user_banned caches for 30s in a module global, so a "not
    # banned" answer from an earlier test would still be warm here.
    from utils import helpers
    helpers._ban_cache.clear()


def make_user(balance="25.00", lang="en"):
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance),
                    language=lang)
        session.add(user)
        session.flush()
        return user.id


def add_txn(user_id, amount, status, method=PaymentMethod.BINANCE_PAY, days_ago=0):
    with get_db_session() as session:
        session.add(Transaction(
            user_id=user_id, amount=Decimal(amount), payment_method=method,
            status=status, created_at=datetime.utcnow() - timedelta(days=days_ago)))


async def press(handler, data, user_id=TELEGRAM_ID):
    query = FakeQuery(data=data, user_id=user_id)
    await handler(FakeUpdate(query, user_id), FakeContext())
    return query


# ---------------------------------------------------------------- wallet

async def test_wallet_shows_the_balance():
    make_user(balance="42.50")

    query = await press(uh.wallet_callback, "wallet")

    assert "$42.50" in query.last_edit_text


async def test_a_payment_being_verified_is_visible():
    """The whole point: the customer has paid and can see it is moving."""
    user_id = make_user()
    add_txn(user_id, "10.00", TransactionStatus.VERIFYING)

    query = await press(uh.wallet_callback, "wallet")

    assert "In progress" in query.last_edit_text
    assert "checking your payment" in query.last_edit_text
    assert "$10.00" in query.last_edit_text


async def test_a_payment_under_manual_review_says_so():
    """Otherwise it looks like the money vanished."""
    user_id = make_user()
    add_txn(user_id, "5.00", TransactionStatus.MANUAL_REVIEW)

    query = await press(uh.wallet_callback, "wallet")

    assert "under review by our team" in query.last_edit_text


async def test_settled_payments_are_listed_separately_from_pending():
    user_id = make_user()
    add_txn(user_id, "10.00", TransactionStatus.VERIFYING)
    add_txn(user_id, "50.00", TransactionStatus.COMPLETED, days_ago=2)

    text = (await press(uh.wallet_callback, "wallet")).last_edit_text

    assert text.index("In progress") < text.index("Recent top-ups")
    assert text.index("$10.00") < text.index("$50.00")


async def test_a_new_customer_sees_an_empty_history_not_a_blank_screen():
    make_user()

    query = await press(uh.wallet_callback, "wallet")

    assert "No top-ups yet" in query.last_edit_text


async def test_the_history_is_capped():
    """The screen should stay one glance."""
    user_id = make_user()
    for i in range(12):
        add_txn(user_id, "1.00", TransactionStatus.COMPLETED, days_ago=i)

    text = (await press(uh.wallet_callback, "wallet")).last_edit_text

    assert text.count("added to your balance") == uh._WALLET_HISTORY


async def test_wallet_offers_top_up_and_back():
    make_user()

    query = await press(uh.wallet_callback, "wallet")

    targets = [b.callback_data for row in query.edits[-1][1].inline_keyboard
               for b in row]
    assert targets == ["topup", "main_menu"]


async def test_wallet_is_translated():
    make_user(lang="bn")

    query = await press(uh.wallet_callback, "wallet")

    assert "ওয়ালেট" in query.last_edit_text
    assert "ব্যালেন্স" in query.last_edit_text


async def test_a_banned_user_gets_nothing():
    make_user()
    with get_db_session() as session:
        session.query(User).filter_by(telegram_id=TELEGRAM_ID).update(
            {"is_banned": True})

    query = await press(uh.wallet_callback, "wallet")

    assert "$25.00" not in query.last_edit_text


async def test_one_customer_never_sees_another_customers_payments():
    mine = make_user()
    with get_db_session() as session:
        other = User(telegram_id=999111, wallet_balance=Decimal("0"))
        session.add(other)
        session.flush()
        other_id = other.id
    add_txn(mine, "10.00", TransactionStatus.COMPLETED)
    add_txn(other_id, "9999.00", TransactionStatus.COMPLETED)

    query = await press(uh.wallet_callback, "wallet")

    assert "$9999.00" not in query.last_edit_text
    assert "$10.00" in query.last_edit_text


# ----------------------------------------------------------------- terms

def test_the_terms_button_is_hidden_until_a_store_writes_some():
    """A button leading to an empty page is worse than no button."""
    labels = [b.callback_data for row in
              create_main_menu_keyboard().inline_keyboard for b in row]

    assert "terms" not in labels


async def test_writing_terms_reveals_the_button():
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "Refunds within 24h.", 1)

    labels = [b.callback_data for row in
              create_main_menu_keyboard().inline_keyboard for b in row]
    assert "terms" in labels


async def test_the_terms_screen_shows_what_the_admin_wrote():
    make_user()
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "Warranty: 30 days.", 1)

    query = await press(uh.terms_callback, "terms")

    assert "Warranty: 30 days." in query.last_edit_text


async def test_clearing_terms_hides_the_button_again():
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "Something", 1)
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "", 1)

    labels = [b.callback_data for row in
              create_main_menu_keyboard().inline_keyboard for b in row]
    assert "terms" not in labels


async def test_whitespace_only_terms_do_not_count_as_written():
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "   \n  ", 1)

    assert store_content.has_terms() is False
    with get_db_session() as session:
        assert session.query(StoreSettings).first().terms_text is None


async def test_setting_terms_is_audit_logged():
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "Refund policy", 4242)

    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="store_terms_set", admin_telegram_id=4242).count() == 1


async def test_the_flag_survives_a_restart():
    await __import__("asyncio").to_thread(
        store_content.set_terms_sync, "Refund policy", 1)

    store_content._has_terms = False          # as a fresh process starts
    assert await __import__("asyncio").to_thread(store_content.read_sync) is True
