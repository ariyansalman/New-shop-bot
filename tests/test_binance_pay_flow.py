"""Binance Pay: checkout, Submit Order ID, and automatic verification.

Driven through the real handlers with fake Telegram objects against a real
(SQLite) database, like the rest of the suite. The provider is the mock
from services/binance_pay_mock.py, so every branch is exercised through
the real verification rules with the network replaced.
"""

from decimal import Decimal

import pytest
from telegram.ext import ConversationHandler

from config.settings import settings
from database import (
    get_db_session, User, Transaction, TransactionStatus, PaymentMethod
)
from handlers import binance_pay_handlers as bp
from fakes import FakeUpdate, FakeQuery, FakeContext, FakeMessage, FakeMessageUpdate

TELEGRAM_ID = 909001


@pytest.fixture(autouse=True)
def binance_enabled(monkeypatch):
    """Turn Binance Pay on in test mode for every test in this module."""
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_TEST_MODE", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "123456789", raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_CURRENCY", "USDT", raising=False)
    monkeypatch.setattr(settings, "BINANCE_MAX_VERIFY_ATTEMPTS", 3, raising=False)


def make_user(balance="0.00") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def make_txn(user_id, amount="10.00", status=TransactionStatus.PENDING,
             provider_txn_id=None, expires_in_hours=1, attempts=0) -> int:
    from utils import calculate_expiry_time
    with get_db_session() as session:
        txn = Transaction(
            user_id=user_id, amount=Decimal(amount),
            payment_method=PaymentMethod.BINANCE_PAY, status=status,
            provider=bp.PROVIDER, provider_transaction_id=provider_txn_id,
            verification_attempts=attempts,
            expires_at=calculate_expiry_time(expires_in_hours),
        )
        session.add(txn)
        session.flush()
        return txn.id


def balance_of(user_id) -> Decimal:
    with get_db_session() as session:
        return session.query(User).filter_by(id=user_id).first().wallet_balance


# ----------------------------------------------------------------------
# Checkout
# ----------------------------------------------------------------------

async def test_checkout_creates_pending_transaction_with_exact_ui():
    make_user()
    query = FakeQuery(data="pay_binance", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['topup_amount'] = Decimal("25.00")

    await bp.payment_method_binance(update, context)

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(
            payment_method=PaymentMethod.BINANCE_PAY).one()
        assert txn.status == TransactionStatus.PENDING
        assert txn.amount == Decimal("25.00")
        assert txn.expires_at is not None          # existing expiry mechanism
        assert txn.provider == "binance"
        assert txn.provider_transaction_id is None  # nothing submitted yet
        txn_id = txn.id

    text = query.last_edit_text
    assert "🟡 BINANCE PAY CHECKOUT" in text
    assert "💰 Amount Due: 25.00 USDT" in text
    assert f"🆔 Order ID: #{txn_id}" in text        # LOCAL id, not a provider id
    assert "🔢 Binance Pay ID: 123456789" in text
    assert "📌 PAYMENT INSTRUCTIONS" in text

    labels = [b.text for row in query.edits[-1][1].inline_keyboard for b in row]
    assert labels == ["🧾 Submit Order ID", "❌ Cancel Order"]


async def test_binance_hidden_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", False, raising=False)
    from utils.keyboards import create_payment_method_keyboard
    callbacks = [b.callback_data for row in create_payment_method_keyboard().inline_keyboard for b in row]
    assert "pay_binance" not in callbacks
    # the existing methods are untouched
    assert "pay_crypto" in callbacks and "pay_card" in callbacks


async def test_binance_shown_when_configured():
    from utils.keyboards import create_payment_method_keyboard
    callbacks = [b.callback_data for row in create_payment_method_keyboard().inline_keyboard for b in row]
    assert "pay_binance" in callbacks
    assert "pay_crypto" in callbacks and "pay_card" in callbacks


# ----------------------------------------------------------------------
# Submit Order ID screen - the two rules the spec calls critical
# ----------------------------------------------------------------------

async def test_submit_order_id_screen_has_no_buttons_at_all():
    user_id = make_user()
    txn_id = make_txn(user_id)
    query = FakeQuery(data=f"binance_submit_{txn_id}", user_id=TELEGRAM_ID)
    update = FakeUpdate(query, TELEGRAM_ID)
    context = FakeContext()

    state = await bp.binance_submit_start(update, context)

    assert state == bp.BINANCE_ORDER_ID
    text, markup = query.edits[-1]
    assert "🧾 ENTER BINANCE ORDER ID" in text
    # No second "Verify Payment" button, and no "Cancel Order" button here.
    assert markup is None, "the order-id input screen must render no keyboard"


# ----------------------------------------------------------------------
# Submitting the id IS the verification trigger
# ----------------------------------------------------------------------

async def test_submitting_id_verifies_and_credits_without_another_button():
    user_id = make_user(balance="5.00")
    txn_id = make_txn(user_id, amount="25.00")

    message = FakeMessage(text="MOCK-SUCCESS-25.00")
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    state = await bp.binance_order_id_received(update, context)

    # "verifying" first, then the result - no button in between.
    assert "🔄 VERIFYING PAYMENT" in message.replies[0][0]
    assert "✅ PAYMENT VERIFIED" in message.last_reply_text
    assert "💰 Amount Added: 25.00 USDT" in message.last_reply_text
    assert f"🆔 Order ID: #{txn_id}" in message.last_reply_text

    assert balance_of(user_id) == Decimal("30.00")   # 5 + 25
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).one()
        assert txn.status == TransactionStatus.COMPLETED
        assert txn.completed_at is not None
        assert txn.provider_transaction_id == "MOCK-SUCCESS-25.00"
    assert state == ConversationHandler.END


async def test_amount_mismatch_does_not_credit():
    user_id = make_user(balance="5.00")
    txn_id = make_txn(user_id, amount="99.00")

    message = FakeMessage(text="MOCK-SUCCESS")     # mock pays 10.00
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(update, context)

    assert "❌ PAYMENT VERIFICATION FAILED" in message.last_reply_text
    assert balance_of(user_id) == Decimal("5.00")
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).one()
        assert txn.status == TransactionStatus.FAILED
        assert "amount mismatch" in txn.last_verification_error


async def test_asset_mismatch_does_not_credit():
    user_id = make_user(balance="5.00")
    txn_id = make_txn(user_id, amount="10.00")
    message = FakeMessage(text="MOCK-WRONG-ASSET")
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(update, context)

    assert "❌ PAYMENT VERIFICATION FAILED" in message.last_reply_text
    assert balance_of(user_id) == Decimal("5.00")


async def test_refund_record_is_not_treated_as_payment():
    user_id = make_user(balance="5.00")
    txn_id = make_txn(user_id, amount="10.00")
    message = FakeMessage(text="MOCK-REFUND")
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(update, context)

    assert balance_of(user_id) == Decimal("5.00")


async def test_temporary_error_keeps_pending_and_does_not_credit():
    user_id = make_user(balance="5.00")
    txn_id = make_txn(user_id, amount="10.00")
    message = FakeMessage(text="MOCK-TIMEOUT")
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(update, context)

    assert "⏳ PAYMENT VERIFICATION PENDING" in message.last_reply_text
    assert balance_of(user_id) == Decimal("5.00")
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).one()
        assert txn.status == TransactionStatus.PENDING   # retryable
        assert txn.verification_attempts == 1


async def test_unknown_id_is_temporary_not_a_rejection():
    """An id that is simply wrong is indistinguishable from one whose
    payment has not landed in history yet, so it must not credit and must
    not permanently fail on the first try."""
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00")
    message = FakeMessage(text="999999999999")
    update = FakeMessageUpdate(message, TELEGRAM_ID)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(update, context)

    assert "⏳ PAYMENT VERIFICATION PENDING" in message.last_reply_text
    assert balance_of(user_id) == Decimal("0.00")


# ----------------------------------------------------------------------
# Idempotency / double credit
# ----------------------------------------------------------------------

async def test_same_id_submitted_twice_credits_once():
    user_id = make_user(balance="0.00")
    txn_id = make_txn(user_id, amount="25.00")
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    m1 = FakeMessage(text="MOCK-SUCCESS-25.00")
    await bp.binance_order_id_received(FakeMessageUpdate(m1, TELEGRAM_ID), context)
    assert balance_of(user_id) == Decimal("25.00")

    # Same id, same order, submitted again.
    context.user_data['binance_transaction_id'] = txn_id
    m2 = FakeMessage(text="MOCK-SUCCESS-25.00")
    await bp.binance_order_id_received(FakeMessageUpdate(m2, TELEGRAM_ID), context)

    assert "ALREADY PROCESSED" in m2.last_reply_text
    assert balance_of(user_id) == Decimal("25.00")    # not 50


async def test_same_provider_id_cannot_settle_a_second_local_order():
    user_id = make_user(balance="0.00")
    first = make_txn(user_id, amount="25.00")
    second = make_txn(user_id, amount="25.00")

    context = FakeContext()
    context.user_data['binance_transaction_id'] = first
    m1 = FakeMessage(text="MOCK-SUCCESS-25.00")
    await bp.binance_order_id_received(FakeMessageUpdate(m1, TELEGRAM_ID), context)
    assert balance_of(user_id) == Decimal("25.00")

    # Same Binance payment, different local order.
    context.user_data['binance_transaction_id'] = second
    m2 = FakeMessage(text="MOCK-SUCCESS-25.00")
    await bp.binance_order_id_received(FakeMessageUpdate(m2, TELEGRAM_ID), context)

    assert "FAILED" in m2.last_reply_text
    assert balance_of(user_id) == Decimal("25.00")    # still not doubled
    with get_db_session() as session:
        assert session.query(Transaction).filter_by(id=second).one().status != TransactionStatus.COMPLETED


async def test_settle_is_idempotent_at_the_lowest_level():
    """Directly calling the credit function twice must credit once."""
    user_id = make_user(balance="0.00")
    txn_id = make_txn(user_id, amount="10.00", provider_txn_id="X-1")

    assert bp._settle_success_sync(txn_id) == "ok"
    assert bp._settle_success_sync(txn_id) == "already"
    assert balance_of(user_id) == Decimal("10.00")


async def test_verification_in_progress_blocks_a_second_submission():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00", status=TransactionStatus.VERIFYING)
    message = FakeMessage(text="MOCK-SUCCESS")
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(FakeMessageUpdate(message, TELEGRAM_ID), context)

    assert "already running" in message.last_reply_text
    assert balance_of(user_id) == Decimal("0.00")


# ----------------------------------------------------------------------
# Expiry, ownership, cancel
# ----------------------------------------------------------------------

async def test_expired_order_cannot_be_paid():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00", expires_in_hours=-1)
    message = FakeMessage(text="MOCK-SUCCESS")
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(FakeMessageUpdate(message, TELEGRAM_ID), context)

    assert "expired" in message.last_reply_text.lower()
    assert balance_of(user_id) == Decimal("0.00")
    with get_db_session() as session:
        assert session.query(Transaction).filter_by(id=txn_id).one().status == TransactionStatus.EXPIRED


async def test_cannot_submit_against_someone_elses_order():
    owner = make_user()
    txn_id = make_txn(owner, amount="10.00")
    with get_db_session() as session:
        session.add(User(telegram_id=777999, wallet_balance=Decimal("0")))

    message = FakeMessage(text="MOCK-SUCCESS")
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    await bp.binance_order_id_received(FakeMessageUpdate(message, 777999), context)

    assert "not found" in message.last_reply_text.lower()
    assert balance_of(owner) == Decimal("0.00")


async def test_cancel_order_marks_failed_and_does_not_credit():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00")
    query = FakeQuery(data=f"binance_cancel_{txn_id}", user_id=TELEGRAM_ID)

    await bp.binance_cancel_order(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "cancelled" in query.last_edit_text.lower()
    with get_db_session() as session:
        assert session.query(Transaction).filter_by(id=txn_id).one().status == TransactionStatus.FAILED
    assert balance_of(user_id) == Decimal("0.00")


async def test_cancel_after_completion_does_not_undo_the_credit():
    user_id = make_user(balance="25.00")
    txn_id = make_txn(user_id, amount="25.00", status=TransactionStatus.COMPLETED)
    query = FakeQuery(data=f"binance_cancel_{txn_id}", user_id=TELEGRAM_ID)

    await bp.binance_cancel_order(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert "ALREADY PROCESSED" in query.last_edit_text
    assert balance_of(user_id) == Decimal("25.00")


async def test_garbage_input_reprompts_without_calling_the_provider():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00")
    message = FakeMessage(text="x" * 200)
    context = FakeContext()
    context.user_data['binance_transaction_id'] = txn_id

    state = await bp.binance_order_id_received(FakeMessageUpdate(message, TELEGRAM_ID), context)

    assert state == bp.BINANCE_ORDER_ID     # stays on the input screen
    assert "does not look like" in message.last_reply_text
    with get_db_session() as session:
        assert session.query(Transaction).filter_by(id=txn_id).one().verification_attempts == 0
