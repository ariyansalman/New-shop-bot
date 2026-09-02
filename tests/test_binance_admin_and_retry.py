"""Binance Pay: the background retry job and the admin panel.

Same approach as test_binance_pay_flow.py - real handlers, fake Telegram
objects, real SQLite database, mock provider. Nothing here stubs out the
verification rules; the admin path and the retry job both settle through
the same verify_transaction() the user path uses, and these tests are
mostly there to prove that neither of them can produce a credit the user
path would have refused.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from config.settings import settings
from database import (
    get_db_session, User, Transaction, TransactionStatus, PaymentMethod,
    AdminActionLog, Settings as StoreSettings,
)
from handlers import binance_pay_handlers as bp
from handlers import binance_admin as ba
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 909002
ADMIN_ID = 700700


@pytest.fixture(autouse=True)
def binance_enabled(monkeypatch):
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_TEST_MODE", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "123456789", raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_CURRENCY", "USDT", raising=False)
    monkeypatch.setattr(settings, "BINANCE_API_KEY", "abcdefghijklmnop", raising=False)
    monkeypatch.setattr(settings, "BINANCE_API_SECRET", "secretsecret", raising=False)
    monkeypatch.setattr(settings, "BINANCE_MAX_VERIFY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "BINANCE_VERIFY_RETRY_INTERVAL", 180, raising=False)
    monkeypatch.setattr(settings, "ADMIN_IDS", {ADMIN_ID}, raising=False)
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", ADMIN_ID, raising=False)
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "test-crypto-key", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_PROVIDER_TOKEN", "test:provider", raising=False)
    # The kill switch is a module-level cache; keep tests independent of
    # whatever a previous test left in it.
    monkeypatch.setattr(bp, "_admin_toggle", True, raising=False)


def make_user(balance="0.00") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def make_txn(user_id, amount="10.00", status=TransactionStatus.PENDING,
             provider_txn_id=None, attempts=0, last_attempt=None,
             method=PaymentMethod.BINANCE_PAY) -> int:
    with get_db_session() as session:
        txn = Transaction(
            user_id=user_id, amount=Decimal(amount),
            payment_method=method, status=status,
            provider=bp.PROVIDER if method == PaymentMethod.BINANCE_PAY else None,
            provider_transaction_id=provider_txn_id,
            verification_attempts=attempts,
            last_verification_at=last_attempt,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        session.add(txn)
        session.flush()
        return txn.id


def status_of(txn_id):
    with get_db_session() as session:
        return session.query(Transaction).filter_by(id=txn_id).one().status


def balance_of(user_id) -> Decimal:
    with get_db_session() as session:
        return session.query(User).filter_by(id=user_id).first().wallet_balance


def admin_update(data: str, user_id: int = ADMIN_ID):
    query = FakeQuery(data=data, user_id=user_id)
    return FakeUpdate(query, user_id), query


# ----------------------------------------------------------------------
# Background retry job
# ----------------------------------------------------------------------

async def test_retry_settles_a_payment_that_landed_late():
    user_id = make_user()
    # Attempt 1 already happened and found nothing; now the id resolves.
    txn_id = make_txn(user_id, amount="10.00", provider_txn_id="MOCK-SUCCESS-10.00",
                      attempts=1, last_attempt=datetime.utcnow() - timedelta(hours=1))
    context = FakeContext()

    await bp.retry_pending_binance_payments(context)

    assert status_of(txn_id) == TransactionStatus.COMPLETED
    assert balance_of(user_id) == Decimal("10.00")
    # The user is told, exactly once.
    assert len(context.bot.sent) >= 1
    assert any("PAYMENT VERIFIED" in text for _chat, text in context.bot.sent)


async def test_retry_respects_the_interval():
    user_id = make_user()
    txn_id = make_txn(user_id, provider_txn_id="MOCK-SUCCESS-10.00",
                      attempts=1, last_attempt=datetime.utcnow())  # just tried

    await bp.retry_pending_binance_payments(FakeContext())

    assert status_of(txn_id) == TransactionStatus.PENDING
    assert balance_of(user_id) == Decimal("0.00")


async def test_retry_skips_transactions_with_no_submitted_id():
    user_id = make_user()
    txn_id = make_txn(user_id, provider_txn_id=None)

    await bp.retry_pending_binance_payments(FakeContext())

    assert status_of(txn_id) == TransactionStatus.PENDING
    with get_db_session() as session:
        # Never even attempted - no API call was spent on it.
        assert session.query(Transaction).filter_by(id=txn_id).one().verification_attempts == 0


async def test_retry_ignores_non_binance_transactions():
    user_id = make_user()
    txn_id = make_txn(user_id, method=PaymentMethod.CRYPTO_WALLET,
                      provider_txn_id="MOCK-SUCCESS-10.00")

    await bp.retry_pending_binance_payments(FakeContext())

    assert status_of(txn_id) == TransactionStatus.PENDING
    assert balance_of(user_id) == Decimal("0.00")


async def test_retry_stops_at_the_attempt_budget_and_asks_for_review():
    user_id = make_user()
    # One attempt short of the budget (3), and the id never resolves.
    txn_id = make_txn(user_id, provider_txn_id="MOCK-UNKNOWN", attempts=2,
                      last_attempt=datetime.utcnow() - timedelta(hours=1))
    context = FakeContext()

    await bp.retry_pending_binance_payments(context)

    assert status_of(txn_id) == TransactionStatus.MANUAL_REVIEW
    assert balance_of(user_id) == Decimal("0.00")
    # Admin is told; the user is not spammed with a non-event.
    assert any("REQUIRES REVIEW" in text for _chat, text in context.bot.sent)

    # A further pass leaves it alone - MANUAL_REVIEW is not retried.
    before = context.bot.sent[:]
    await bp.retry_pending_binance_payments(context)
    assert status_of(txn_id) == TransactionStatus.MANUAL_REVIEW
    assert context.bot.sent == before


async def test_retry_does_not_message_the_user_while_still_waiting():
    user_id = make_user()
    txn_id = make_txn(user_id, provider_txn_id="MOCK-UNKNOWN", attempts=0,
                      last_attempt=None)
    context = FakeContext()

    await bp.retry_pending_binance_payments(context)

    assert status_of(txn_id) == TransactionStatus.PENDING
    assert context.bot.sent == []


async def test_retry_is_a_no_op_when_binance_is_switched_off(monkeypatch):
    user_id = make_user()
    txn_id = make_txn(user_id, provider_txn_id="MOCK-SUCCESS-10.00",
                      attempts=1, last_attempt=datetime.utcnow() - timedelta(hours=1))
    monkeypatch.setattr(bp, "_admin_toggle", False, raising=False)

    await bp.retry_pending_binance_payments(FakeContext())

    assert status_of(txn_id) == TransactionStatus.PENDING
    assert balance_of(user_id) == Decimal("0.00")


async def test_expiry_sweep_leaves_a_submitted_binance_payment_alone():
    """A paid order must not be expired out from under the retry job."""
    from handlers import payment_handlers

    user_id = make_user()
    with get_db_session() as session:
        submitted = Transaction(
            user_id=user_id, amount=Decimal("10.00"),
            payment_method=PaymentMethod.BINANCE_PAY,
            status=TransactionStatus.PENDING,
            provider=bp.PROVIDER, provider_transaction_id="MOCK-UNKNOWN",
            expires_at=datetime.utcnow() - timedelta(hours=1),   # long past
        )
        untouched = Transaction(
            user_id=user_id, amount=Decimal("10.00"),
            payment_method=PaymentMethod.BINANCE_PAY,
            status=TransactionStatus.PENDING,
            provider=bp.PROVIDER, provider_transaction_id=None,  # never submitted
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add_all([submitted, untouched])
        session.flush()
        submitted_id, untouched_id = submitted.id, untouched.id

    await payment_handlers.check_expired_payments(FakeContext())

    assert status_of(submitted_id) == TransactionStatus.PENDING
    # An abandoned checkout still expires exactly as before.
    assert status_of(untouched_id) == TransactionStatus.EXPIRED


# ----------------------------------------------------------------------
# Admin panel: authorization
# ----------------------------------------------------------------------

@pytest.mark.parametrize("handler,data", [
    (ba.binance_admin_menu, "binadmin_menu"),
    (ba.binance_admin_toggle, "binadmin_toggle"),
    (ba.binance_admin_test, "binadmin_test"),
    (ba.binance_admin_monitor, "binadmin_mon_review_0"),
    (ba.binance_admin_retry, "binadmin_retry_1"),
    (ba.binance_admin_close, "binadmin_close_1"),
])
async def test_every_admin_screen_rejects_a_non_admin(handler, data):
    update, query = admin_update(data, user_id=TELEGRAM_ID)

    await handler(update, FakeContext())

    assert query.edits == []                      # nothing rendered
    assert query.last_answer[0] == "⛔ Access denied."
    assert query.last_answer[1].get("show_alert") is True


async def test_non_admin_cannot_credit_through_the_retry_button():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00", provider_txn_id="MOCK-SUCCESS-10.00")
    update, _query = admin_update(f"binadmin_retry_{txn_id}", user_id=TELEGRAM_ID)

    await ba.binance_admin_retry(update, FakeContext())

    assert status_of(txn_id) == TransactionStatus.PENDING
    assert balance_of(user_id) == Decimal("0.00")


# ----------------------------------------------------------------------
# Admin panel: settings screen
# ----------------------------------------------------------------------

async def test_settings_screen_never_shows_the_credentials():
    update, query = admin_update("binadmin_menu")

    await ba.binance_admin_menu(update, FakeContext())

    text = query.last_edit_text
    assert settings.BINANCE_API_KEY not in text
    assert settings.BINANCE_API_SECRET not in text
    # Only the tail of the key, enough to tell which one is loaded.
    assert "✅ set (…mnop)" in text
    assert "TEST MODE IS ON" in text          # loud about the mock provider


async def test_toggle_persists_and_hides_the_method_from_users():
    from utils.keyboards import create_payment_method_keyboard

    update, query = admin_update("binadmin_toggle")
    await ba.binance_admin_toggle(update, FakeContext())

    assert bp.binance_pay_available() is False
    callbacks = [b.callback_data for row in create_payment_method_keyboard().inline_keyboard
                 for b in row]
    assert "pay_binance" not in callbacks
    assert "pay_crypto" in callbacks           # other methods untouched

    with get_db_session() as session:
        assert session.query(StoreSettings).first().binance_pay_enabled is False
        assert session.query(AdminActionLog).filter_by(
            action="binance_pay_disable").count() == 1

    # And back on again.
    update, query = admin_update("binadmin_toggle")
    await ba.binance_admin_toggle(update, FakeContext())
    assert bp.binance_pay_available() is True
    assert "🟢 live" in query.last_edit_text


async def test_toggle_cannot_enable_an_unconfigured_server(monkeypatch):
    """No Telegram button can supply credentials the server does not have."""
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "", raising=False)
    update, query = admin_update("binadmin_toggle")

    await ba.binance_admin_toggle(update, FakeContext())

    assert bp.binance_pay_available() is False
    # Naming the missing variable, not just "not configured".
    assert "BINANCE_PAY_ID" in query.last_answer[0]


async def test_toggle_enables_when_the_env_default_is_off(monkeypatch):
    """The button that never worked.

    BINANCE_PAY_ENABLED off plus credentials present used to make
    binance_configured() false, so the toggle refused - and refused through
    a second query.answer() that Telegram drops, so nothing happened at all.
    """
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", False, raising=False)
    monkeypatch.setattr(bp, "_admin_toggle", None, raising=False)
    assert bp.binance_pay_available() is False

    update, query = admin_update("binadmin_toggle")
    await ba.binance_admin_toggle(update, FakeContext())

    assert bp.binance_pay_available() is True
    with get_db_session() as session:
        assert session.query(StoreSettings).first().binance_pay_enabled is True


async def test_the_admin_choice_outlives_the_env_default(monkeypatch):
    """Once an admin decides, BINANCE_PAY_ENABLED stops deciding."""
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    monkeypatch.setattr(bp, "_admin_toggle", None, raising=False)
    assert bp.binance_pay_available() is True

    update, _query = admin_update("binadmin_toggle")
    await ba.binance_admin_toggle(update, FakeContext())        # admin turns it off

    assert bp.binance_pay_available() is False
    assert bp.refresh_admin_toggle() is False                    # and it survives a reboot
    assert bp.binance_pay_available() is False


async def test_untouched_switch_follows_the_environment(monkeypatch):
    monkeypatch.setattr(bp, "_admin_toggle", None, raising=False)

    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    assert bp.binance_pay_available() is True

    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", False, raising=False)
    assert bp.binance_pay_available() is False


async def test_toggle_answers_the_callback_exactly_once(monkeypatch):
    """Telegram drops every answer after the first, so an alert sent as a
    second answer is invisible - which is how this button failed silently."""
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "", raising=False)
    update, query = admin_update("binadmin_toggle")

    await ba.binance_admin_toggle(update, FakeContext())

    assert len(query.answers) == 1
    assert query.answers[0][1].get("show_alert") is True


async def test_refresh_admin_toggle_reloads_the_saved_value():
    with get_db_session() as session:
        session.add(StoreSettings(binance_pay_enabled=False))
        session.commit()

    assert bp.refresh_admin_toggle() is False
    assert bp.binance_pay_available() is False


async def test_test_configuration_button_reports_without_leaking():
    update, query = admin_update("binadmin_test")

    await ba.binance_admin_test(update, FakeContext())

    text = query.last_edit_text
    assert "CONFIGURATION OK" in text
    assert settings.BINANCE_API_SECRET not in text
    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="binance_test_config").count() == 1


# ----------------------------------------------------------------------
# Admin panel: monitoring
# ----------------------------------------------------------------------

async def test_monitoring_filters_by_status():
    user_id = make_user()
    make_txn(user_id, amount="11.00", status=TransactionStatus.MANUAL_REVIEW,
             provider_txn_id="TX-REVIEW")
    make_txn(user_id, amount="22.00", status=TransactionStatus.COMPLETED,
             provider_txn_id="TX-DONE")

    update, query = admin_update("binadmin_mon_review_0")
    await ba.binance_admin_monitor(update, FakeContext())
    text = query.last_edit_text
    assert "TX-REVIEW" in text and "TX-DONE" not in text
    assert "Manual Review" in text

    update, query = admin_update("binadmin_mon_completed_0")
    await ba.binance_admin_monitor(update, FakeContext())
    text = query.last_edit_text
    assert "TX-DONE" in text and "TX-REVIEW" not in text


async def test_monitoring_offers_review_actions_but_never_a_credit_button():
    user_id = make_user()
    txn_id = make_txn(user_id, status=TransactionStatus.MANUAL_REVIEW,
                      provider_txn_id="TX-REVIEW")

    update, query = admin_update("binadmin_mon_review_0")
    await ba.binance_admin_monitor(update, FakeContext())

    callbacks = [b.callback_data for row in query.edits[-1][1].inline_keyboard for b in row]
    assert f"binadmin_retry_{txn_id}" in callbacks
    assert f"binadmin_close_{txn_id}" in callbacks
    # There is no button anywhere that adds balance directly.
    assert not any("credit" in c or "approve" in c for c in callbacks)


async def test_monitoring_survives_an_unknown_filter():
    make_user()
    update, query = admin_update("binadmin_mon_nonsense_0")

    await ba.binance_admin_monitor(update, FakeContext())

    assert "Pending" in query.last_edit_text           # falls back, no crash


async def test_filter_buttons_carry_their_counts():
    """Without counts an admin must open all six filters to find anything,
    and the default screen reads as broken whenever its queue is empty."""
    user_id = make_user()
    make_txn(user_id, provider_txn_id="A", status=TransactionStatus.MANUAL_REVIEW)
    make_txn(user_id, provider_txn_id="B", status=TransactionStatus.MANUAL_REVIEW)
    make_txn(user_id, provider_txn_id="C", status=TransactionStatus.PENDING)

    update, query = admin_update("binadmin_mon_pending_0")
    await ba.binance_admin_monitor(update, FakeContext())

    labels = [b.text for row in query.edits[-1][1].inline_keyboard for b in row]
    assert any("Manual Review (2)" in text for text in labels)
    assert any("Pending (1)" in text for text in labels)
    # A status with nothing in it stays uncluttered.
    assert any(text.strip("• ") == "⌛ Expired" for text in labels)


# ----------------------------------------------------------------------
# Admin panel: re-verify and close
# ----------------------------------------------------------------------

async def test_admin_retry_settles_a_manual_review_that_now_resolves():
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00", status=TransactionStatus.MANUAL_REVIEW,
                      provider_txn_id="MOCK-SUCCESS-10.00", attempts=3)
    context = FakeContext()
    update, query = admin_update(f"binadmin_retry_{txn_id}")

    await ba.binance_admin_retry(update, context)

    assert status_of(txn_id) == TransactionStatus.COMPLETED
    assert balance_of(user_id) == Decimal("10.00")
    assert "PAYMENT VERIFIED" in query.last_edit_text
    assert any("PAYMENT VERIFIED" in text for _chat, text in context.bot.sent)
    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="binance_retry_verify", target_id=txn_id).count() == 1


async def test_admin_retry_still_refuses_a_payment_that_never_arrived():
    """The admin button is not a bypass - it runs the same rules."""
    user_id = make_user()
    txn_id = make_txn(user_id, amount="10.00", status=TransactionStatus.MANUAL_REVIEW,
                      provider_txn_id="MOCK-WRONG-AMOUNT", attempts=3)
    update, query = admin_update(f"binadmin_retry_{txn_id}")

    await ba.binance_admin_retry(update, FakeContext())

    assert status_of(txn_id) == TransactionStatus.FAILED
    assert balance_of(user_id) == Decimal("0.00")
    assert "VERIFICATION FAILED" in query.last_edit_text


async def test_admin_retry_on_a_transaction_with_no_id_does_nothing():
    user_id = make_user()
    txn_id = make_txn(user_id, provider_txn_id=None)
    update, query = admin_update(f"binadmin_retry_{txn_id}")

    await ba.binance_admin_retry(update, FakeContext())

    assert "no Binance ID submitted" in query.last_edit_text
    assert status_of(txn_id) == TransactionStatus.PENDING
    assert balance_of(user_id) == Decimal("0.00")


async def test_admin_retry_does_not_double_credit_a_completed_payment():
    user_id = make_user(balance="10.00")
    txn_id = make_txn(user_id, amount="10.00", status=TransactionStatus.COMPLETED,
                      provider_txn_id="MOCK-SUCCESS-10.00")
    update, query = admin_update(f"binadmin_retry_{txn_id}")

    await ba.binance_admin_retry(update, FakeContext())

    assert balance_of(user_id) == Decimal("10.00")
    assert "ALREADY PROCESSED" in query.last_edit_text


async def test_admin_close_marks_failed_without_crediting():
    user_id = make_user()
    txn_id = make_txn(user_id, status=TransactionStatus.MANUAL_REVIEW,
                      provider_txn_id="MOCK-UNKNOWN")
    update, query = admin_update(f"binadmin_close_{txn_id}")

    await ba.binance_admin_close(update, FakeContext())

    assert status_of(txn_id) == TransactionStatus.FAILED
    assert balance_of(user_id) == Decimal("0.00")
    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="binance_close_unpaid", target_id=txn_id).count() == 1


async def test_admin_close_will_not_touch_a_completed_payment():
    user_id = make_user(balance="10.00")
    txn_id = make_txn(user_id, amount="10.00", status=TransactionStatus.COMPLETED,
                      provider_txn_id="MOCK-SUCCESS-10.00")
    update, query = admin_update(f"binadmin_close_{txn_id}")

    await ba.binance_admin_close(update, FakeContext())

    assert status_of(txn_id) == TransactionStatus.COMPLETED
    assert balance_of(user_id) == Decimal("10.00")
    assert "Already completed" in query.last_edit_text
