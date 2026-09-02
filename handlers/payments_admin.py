"""Admin → 💳 Payments: every top-up method behind one button.

The hub lists all three methods with their live state, so an admin can see
at a glance what customers are actually being offered - previously that
meant reading environment variables on the host, since only Binance had a
screen at all.

Each method gets the same controls: an on/off switch and, where the method
has something to test, a connectivity check. Binance keeps its deeper
screens (verification settings, payment monitoring) because it is the only
method whose payments an admin has to chase; those live in binance_admin.

A switch here can only ever narrow what the server can do. Turning a method
on also needs its credentials, which are environment-only and which no
Telegram button can supply - see services/payment_methods.py.
"""

import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from services import payment_methods
from utils import is_admin
from utils.keyboards import two_column_rows

logger = logging.getLogger(__name__)


async def _deny(update: Update) -> bool:
    """Answer with the access-denied alert. Returns True when denied.

    Authorization answers the callback itself, before any other answer:
    Telegram accepts one answer per callback and discards the rest, so a
    handler that answers first would make this alert invisible.
    """
    if is_admin(update.effective_user.id):
        return False
    await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    return True


def _state_line(spec) -> str:
    """One line saying exactly what this method is doing right now."""
    if not spec.configured():
        return "🔴 unusable - missing " + ", ".join(spec.missing())
    if payment_methods.available(spec.key):
        return "🟢 live"
    if payment_methods.switch_state(spec.key) is None:
        return f"⚪️ off by default ({spec.env_var} is not set)"
    return "🟠 switched off by an admin"


# ======================================================================
# Hub
# ======================================================================

def _hub_text() -> str:
    lines = ["💳 PAYMENT METHODS",
             "━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for spec in payment_methods.SPECS:
        lines.append(f"{spec.label}")
        lines.append(f"   {_state_line(spec)}")
        lines.append("")

    live = payment_methods.available_specs()
    if live:
        lines.append("Customers can pay with: "
                     + ", ".join(s.name for s in live))
    else:
        # Worth saying plainly - the top-up flow is closed in this state.
        lines.append("⚠️ No method is live, so customers cannot")
        lines.append("add balance at all.")

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


# Binance's screen is richer than a status page - verification settings and
# payment monitoring - so it keeps its own module and the hub links to it
# rather than showing a near-duplicate here.
_METHOD_SCREEN = {"binance": "binadmin_menu"}


def _hub_keyboard():
    buttons = [InlineKeyboardButton(
        spec.label,
        callback_data=_METHOD_SCREEN.get(spec.key, f"payadmin_{spec.key}"))
        for spec in payment_methods.SPECS]
    keyboard = two_column_rows(buttons)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)


async def payments_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The Payments hub."""
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    try:
        await query.edit_message_text(_hub_text(), reply_markup=_hub_keyboard())
    except Exception:
        # Usually "message is not modified" - the same screen is already up.
        logger.debug("Payments hub not re-rendered", exc_info=True)


# ======================================================================
# One method
# ======================================================================

def _method_text(spec) -> str:
    lines = [spec.label.upper(),
             "━━━━━━━━━━━━━━━━━━━━━━━━", "",
             f"Status: {_state_line(spec)}", ""]

    lines.append("🔐 CONFIGURATION (environment only)")
    if spec.key == "crypto":
        lines.append(f"• API key: {_mask(settings.CRYPTO_BOT_API_KEY)}")
    elif spec.key == "card":
        lines.append(f"• Provider token: {_mask(settings.TELEGRAM_PROVIDER_TOKEN)}")
        lines.append(f"• Currency: {settings.PAYMENT_CURRENCY}")

    if spec.notes:
        lines += [""] + [f"• {note}" for note in spec.notes]

    lines += ["",
              "Credentials live in the server",
              "environment and are never editable",
              "or shown in Telegram. The switch",
              "below takes effect immediately.",
              "",
              "━━━━━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def _mask(value: str) -> str:
    """Report that a secret is set without disclosing it."""
    if not value:
        return "❌ not set"
    if len(value) <= 4:
        return "✅ set"
    return f"✅ set (…{value[-4:]})"


def _method_keyboard(spec):
    on = payment_methods.available(spec.key)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔴 Disable {spec.name}" if on else f"🟢 Enable {spec.name}",
            callback_data=f"payadmin_toggle_{spec.key}")],
        [InlineKeyboardButton("🔙 Back", callback_data="payadmin_menu")],
    ])


def _spec_from(query_data: str, prefix: str):
    return payment_methods.spec(query_data[len(prefix):])


async def payment_method_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One method's screen."""
    query = update.callback_query
    if await _deny(update):
        return

    spec = _spec_from(query.data, "payadmin_")
    if spec is None:
        await query.answer("Unknown payment method.", show_alert=True)
        return
    await query.answer()

    try:
        await query.edit_message_text(_method_text(spec),
                                      reply_markup=_method_keyboard(spec))
    except Exception:
        logger.debug("Payment method screen not re-rendered", exc_info=True)


async def payment_method_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Turn one method on or off.

    One query.answer() per path: Telegram discards every answer after the
    first, so answering up front and then alerting shows the admin nothing
    and the button looks dead.
    """
    query = update.callback_query
    if await _deny(update):
        return

    spec = _spec_from(query.data, "payadmin_toggle_")
    if spec is None:
        await query.answer("Unknown payment method.", show_alert=True)
        return

    if not spec.configured():
        await query.answer(
            f"Cannot enable: the server is missing {', '.join(spec.missing())}. "
            "Set it in the environment and redeploy.", show_alert=True)
        return

    new_value = not payment_methods.available(spec.key)
    try:
        await asyncio.to_thread(payment_methods.set_switch_sync, spec.key,
                                new_value, update.effective_user.id)
    except Exception:
        logger.exception("Could not persist the %s payment switch", spec.key)
        await query.answer("Could not save that. Check the logs.", show_alert=True)
        return

    await query.answer(f"{spec.name} is now {'ON' if new_value else 'OFF'}")
    await query.edit_message_text(_method_text(spec),
                                  reply_markup=_method_keyboard(spec))
