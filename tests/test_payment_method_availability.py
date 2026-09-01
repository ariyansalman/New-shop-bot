"""Only offer a top-up method this deployment can actually complete.

A button that cannot work is worse than no button. CryptoBot was the bad
case: with no API key it still accepted the tap, wrote a PENDING
transaction row, failed against the API, and told the user to "try again"
- advice that could never succeed, leaving a dead row behind each time.
"""

from decimal import Decimal

import pytest

from config.settings import settings
from database import get_db_session, User, Transaction
from handlers import payment_handlers as ph
from handlers import binance_pay_handlers as bp
from utils.keyboards import create_payment_method_keyboard, payment_methods_available
from fakes import FakeUpdate, FakeQuery, FakeContext

TELEGRAM_ID = 909003


@pytest.fixture(autouse=True)
def no_methods(monkeypatch):
    """Start every test from "nothing configured" and switch things on."""
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_PROVIDER_TOKEN", "", raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", False, raising=False)
    monkeypatch.setattr(bp, "_admin_toggle", True, raising=False)


def enable_binance(monkeypatch):
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_TEST_MODE", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "123456789", raising=False)


def callbacks():
    return [b.callback_data
            for row in create_payment_method_keyboard().inline_keyboard
            for b in row]


def make_user():
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal("0.00"))
        session.add(user)
        session.flush()
        return user.id


def test_unconfigured_methods_are_not_offered():
    assert payment_methods_available() == []
    assert callbacks() == ["cancel"]


def test_each_method_appears_only_once_configured(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)
    assert callbacks() == ["pay_crypto", "cancel"]

    monkeypatch.setattr(settings, "TELEGRAM_PROVIDER_TOKEN", "tok", raising=False)
    assert callbacks() == ["pay_crypto", "pay_card", "cancel"]

    enable_binance(monkeypatch)
    assert callbacks() == ["pay_crypto", "pay_card", "pay_binance", "cancel"]


def test_binance_only_deployment(monkeypatch):
    """The configuration this store is heading for."""
    enable_binance(monkeypatch)
    assert callbacks() == ["pay_binance", "cancel"]


async def test_topup_refuses_early_when_nothing_is_configured():
    """Do not ask for an amount and then show a menu of only Cancel."""
    make_user()
    query = FakeQuery(data="topup", user_id=TELEGRAM_ID)

    state = await ph.topup_start(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    from telegram.ext import ConversationHandler
    assert state == ConversationHandler.END
    assert "unavailable" in query.last_edit_text.lower()


async def test_topup_still_starts_when_one_method_exists(monkeypatch):
    enable_binance(monkeypatch)
    make_user()
    query = FakeQuery(data="topup", user_id=TELEGRAM_ID)

    state = await ph.topup_start(FakeUpdate(query, TELEGRAM_ID), FakeContext())

    assert state == ph.AMOUNT
    assert "amount" in query.last_edit_text.lower()


async def test_crypto_without_a_key_writes_no_transaction_row():
    """The regression this guards: a dead PENDING row per attempt."""
    make_user()
    query = FakeQuery(data="pay_crypto", user_id=TELEGRAM_ID)
    context = FakeContext()
    context.user_data['topup_amount'] = Decimal("25.00")

    await ph.payment_method_crypto(FakeUpdate(query, TELEGRAM_ID), context)

    assert "not configured" in query.last_edit_text
    # Nothing was written, so nothing needs cleaning up later.
    with get_db_session() as session:
        assert session.query(Transaction).count() == 0
