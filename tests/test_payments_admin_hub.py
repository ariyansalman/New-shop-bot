"""Admin → 💳 Payments: every method behind one button.

Before this hub, only Binance had an admin screen; CryptoBot and Card
could be seen and changed only by reading environment variables on the
host. The hub is also the one place that says plainly when no method is
live, which closes the top-up flow entirely.
"""

import pytest

from config.settings import settings
from database import get_db_session, Settings as StoreSettings, AdminActionLog
from services import payment_methods
from handlers import payments_admin as pa
from utils.keyboards import create_admin_main_menu_keyboard
from fakes import FakeUpdate, FakeQuery, FakeContext, set_switch

ADMIN_ID = 700701


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Nothing configured, nothing decided, admin recognised."""
    monkeypatch.setattr(settings, "ADMIN_IDS", {ADMIN_ID}, raising=False)
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", ADMIN_ID, raising=False)
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_PROVIDER_TOKEN", "", raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "", raising=False)
    for key in ("crypto", "card", "binance"):
        set_switch(key, None, monkeypatch)


async def press(handler, data, user_id=ADMIN_ID):
    query = FakeQuery(data=data, user_id=user_id)
    await handler(FakeUpdate(query, user_id), FakeContext())
    return query


def buttons(query):
    return [b.text for row in query.edits[-1][1].inline_keyboard for b in row]


# ------------------------------------------------------------ the button

def test_the_admin_menu_has_one_payments_button():
    labels = [b.text for row in create_admin_main_menu_keyboard().inline_keyboard
              for b in row]
    assert "💳 Payments" in labels
    # The old top-level Binance entry is gone - it lives inside the hub now.
    assert "🟡 Binance Pay" not in labels


# ------------------------------------------------------------------- hub

async def test_hub_lists_every_method():
    query = await press(pa.payments_menu, "payadmin_menu")

    for spec in payment_methods.SPECS:
        assert spec.label in query.last_edit_text


async def test_hub_says_when_no_method_is_live():
    """This state closes the top-up flow, so it must not be silent."""
    query = await press(pa.payments_menu, "payadmin_menu")

    assert "No method is live" in query.last_edit_text


async def test_hub_names_the_missing_variable_per_method():
    query = await press(pa.payments_menu, "payadmin_menu")

    assert "CRYPTO_BOT_API_KEY" in query.last_edit_text
    assert "TELEGRAM_PROVIDER_TOKEN" in query.last_edit_text


async def test_hub_lists_the_live_methods(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)

    query = await press(pa.payments_menu, "payadmin_menu")

    assert "Customers can pay with: CryptoBot" in query.last_edit_text
    assert "No method is live" not in query.last_edit_text


async def test_binance_opens_its_own_richer_screen():
    """Verification settings and monitoring live there, so the hub links
    to it rather than showing a near-duplicate."""
    query = await press(pa.payments_menu, "payadmin_menu")

    targets = {b.text: b.callback_data
               for row in query.edits[-1][1].inline_keyboard for b in row}
    assert targets["🟡 Binance Pay"] == "binadmin_menu"
    assert targets["🪙 CryptoBot"] == "payadmin_crypto"


# --------------------------------------------------------- method screen

async def test_method_screen_masks_the_credential(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "12345:supersecret",
                        raising=False)

    query = await press(pa.payment_method_detail, "payadmin_crypto")

    assert "12345:supersecret" not in query.last_edit_text
    assert "✅ set (…cret)" in query.last_edit_text


async def test_unknown_method_is_refused():
    query = await press(pa.payment_method_detail, "payadmin_nonsense")

    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits


# --------------------------------------------------------------- toggles

async def test_toggle_turns_a_configured_method_on(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)
    set_switch("crypto", False, monkeypatch)
    assert payment_methods.available("crypto") is False

    query = await press(pa.payment_method_toggle, "payadmin_toggle_crypto")

    assert payment_methods.available("crypto") is True
    assert "is now ON" in query.answers[0][0]
    with get_db_session() as session:
        assert session.query(StoreSettings).first().crypto_pay_enabled is True


async def test_toggle_hides_the_method_from_customers(monkeypatch):
    from utils.keyboards import create_payment_method_keyboard
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_PROVIDER_TOKEN", "tok", raising=False)

    await press(pa.payment_method_toggle, "payadmin_toggle_crypto")

    callbacks = [b.callback_data
                 for row in create_payment_method_keyboard().inline_keyboard
                 for b in row]
    assert "pay_crypto" not in callbacks
    assert "pay_card" in callbacks          # the others are untouched


async def test_toggle_cannot_enable_a_method_with_no_credentials():
    """No Telegram button can supply a credential the server lacks."""
    query = await press(pa.payment_method_toggle, "payadmin_toggle_card")

    assert payment_methods.available("card") is False
    assert "TELEGRAM_PROVIDER_TOKEN" in query.answers[0][0]
    assert query.answers[0][1].get("show_alert") is True


async def test_toggle_answers_the_callback_exactly_once():
    """Telegram drops every answer after the first, so a refusal sent as a
    second answer is invisible and the button looks dead."""
    query = await press(pa.payment_method_toggle, "payadmin_toggle_card")

    assert len(query.answers) == 1


async def test_toggle_is_audit_logged(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)

    await press(pa.payment_method_toggle, "payadmin_toggle_crypto")

    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="payment_crypto_disable").count() == 1


async def test_a_non_admin_gets_nothing(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)

    query = await press(pa.payment_method_toggle, "payadmin_toggle_crypto",
                        user_id=999999)

    assert payment_methods.available("crypto") is True    # unchanged
    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits


# ------------------------------------------------------ switch semantics

async def test_an_untouched_switch_follows_the_environment(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)
    assert payment_methods.available("crypto") is True   # key present = on

    set_switch("crypto", False, monkeypatch)
    assert payment_methods.available("crypto") is False  # admin decided


async def test_the_switch_survives_a_restart(monkeypatch):
    monkeypatch.setattr(settings, "CRYPTO_BOT_API_KEY", "key", raising=False)
    await press(pa.payment_method_toggle, "payadmin_toggle_crypto")

    payment_methods.refresh()                            # as a reboot would

    assert payment_methods.available("crypto") is False
