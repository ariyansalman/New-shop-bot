"""Binance Pay admin panel.

Two screens, both reachable from the existing admin menu and both gated by
the existing is_admin() check:

  * Settings   - configuration status, the on/off switch, a connectivity test
  * Monitoring - Binance transactions filtered by status, with a manual
                 re-verify and a way to close out a stuck one

The credentials are never shown, never editable from here, and never
logged. They live in the environment (see config/settings.py for why), so
this screen reports only whether each one is set, plus the last four
characters of the API key - enough for an admin to tell which key is
loaded, not enough to use it.

The re-verify button runs the same verify_transaction() the user flow and
the retry job run. There is deliberately no "credit this manually"
button here: an admin who genuinely needs to hand out balance already has
the existing manual-confirm screens, and those are audit-logged as what
they are rather than disguised as a verified Binance payment.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from sqlalchemy import func

from database import get_db_session, Transaction, TransactionStatus, PaymentMethod
from config.settings import settings
from handlers import binance_pay_handlers as bp
from services.binance_pay import get_service
from utils import is_admin, notify_admin
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

# Status filters offered on the monitoring screen, in the order shown.
_FILTERS = [
    ("pending", "⏳ Pending", TransactionStatus.PENDING),
    ("verifying", "🔄 Verifying", TransactionStatus.VERIFYING),
    ("completed", "✅ Completed", TransactionStatus.COMPLETED),
    ("failed", "❌ Failed", TransactionStatus.FAILED),
    ("expired", "⌛ Expired", TransactionStatus.EXPIRED),
    ("review", "🟡 Manual Review", TransactionStatus.MANUAL_REVIEW),
]
_FILTER_BY_KEY = {key: (label, status) for key, label, status in _FILTERS}

_PAGE_SIZE = 8


async def _deny(update: Update) -> bool:
    """Answer with the access-denied alert. Returns True when denied.

    Authorization happens before query.answer(), because Telegram rejects a
    second answer for the same callback and the alert would never appear.
    """
    if is_admin(update.effective_user.id):
        return False
    await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    return True


def _mask(value: str) -> str:
    """Report that a secret is set without disclosing it."""
    if not value:
        return "❌ not set"
    if len(value) <= 4:
        return "✅ set"
    return f"✅ set (…{value[-4:]})"


def _missing_credentials() -> list:
    """Which environment values are missing, for a message an admin can act on.

    "not configured" alone leaves the admin guessing which of four variables
    it means.
    """
    missing = []
    if not settings.BINANCE_PAY_ID:
        missing.append("BINANCE_PAY_ID")
    if not settings.BINANCE_TEST_MODE:
        if not settings.BINANCE_API_KEY:
            missing.append("BINANCE_API_KEY")
        if not settings.BINANCE_API_SECRET:
            missing.append("BINANCE_API_SECRET")
    return missing


# ======================================================================
# Settings screen
# ======================================================================

def _settings_text() -> str:
    if not bp.binance_configured():
        state = "🔴 unusable - missing " + ", ".join(_missing_credentials())
    elif bp.binance_pay_available():
        state = "🟢 live - customers can pay with Binance"
    elif bp._admin_toggle is None:
        state = "⚪️ off by default (BINANCE_PAY_ENABLED is not set)"
    else:
        state = "🟠 switched off by an admin"

    lines = [
        "🟡 BINANCE PAY SETTINGS",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Status: {state}",
        "",
        "🔐 CREDENTIALS (environment only)",
        f"• API Key: {_mask(settings.BINANCE_API_KEY)}",
        f"• API Secret: {_mask(settings.BINANCE_API_SECRET)}",
        f"• Pay ID: {settings.BINANCE_PAY_ID or '❌ not set'}",
        f"• Currency: {settings.BINANCE_PAY_CURRENCY}",
        "",
        "⚙️ VERIFICATION",
        f"• Checkout expiry: {settings.PAYMENT_EXPIRY_HOURS * 60:.0f} min",
        f"• Max attempts: {settings.BINANCE_MAX_VERIFY_ATTEMPTS}",
        f"• Retry interval: {settings.BINANCE_VERIFY_RETRY_INTERVAL}s",
    ]

    if settings.BINANCE_TEST_MODE:
        lines += [
            "",
            "⚠️ TEST MODE IS ON",
            "Payments are checked against a mock,",
            "not the real Binance API. Never leave",
            "this on in production.",
        ]

    lines += [
        "",
        "Credentials are set in the server",
        "environment and are never editable",
        "or shown in Telegram. The switch",
        "below is the live on/off and takes",
        "effect immediately.",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def _settings_keyboard():
    toggle = ("🔴 Disable Binance Pay" if bp.binance_pay_available()
              else "🟢 Enable Binance Pay")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Config", callback_data="binadmin_test"),
         InlineKeyboardButton("📊 Monitoring", callback_data="binadmin_mon_pending_0")],
        # The switch keeps a row to itself: it is the one button here that
        # changes what customers can do, and it should not be a half-width
        # neighbour of something harmless.
        [InlineKeyboardButton(toggle, callback_data="binadmin_toggle")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_menu")],
    ])


async def binance_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Binance Pay settings screen."""
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    try:
        await query.edit_message_text(_settings_text(), reply_markup=_settings_keyboard())
    except Exception:
        # Usually "message is not modified" - the same screen is already up.
        # Logged rather than swallowed so a real edit failure is findable.
        logger.debug("Binance settings screen not re-rendered", exc_info=True)


async def binance_admin_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Turn Binance top-ups on or off.

    Note the single query.answer() per path. Telegram accepts one answer
    per callback and silently drops the rest, so answering up front and
    then answering again with an alert means the admin sees nothing at all
    happen - which is exactly how this button used to fail.
    """
    query = update.callback_query
    if await _deny(update):
        return

    if not bp.binance_configured():
        missing = ", ".join(_missing_credentials())
        await query.answer(
            f"Cannot enable: the server is missing {missing}. "
            "Set it in the environment and redeploy.", show_alert=True)
        return

    new_value = not bp.binance_pay_available()
    admin_id = update.effective_user.id
    try:
        await asyncio.to_thread(bp._write_admin_toggle_sync, new_value, admin_id)
    except Exception:
        logger.exception("Could not persist the Binance switch")
        await query.answer("Could not save that. Check the logs.", show_alert=True)
        return

    await query.answer("Binance Pay is now ON" if new_value
                       else "Binance Pay is now OFF")
    await query.edit_message_text(_settings_text(), reply_markup=_settings_keyboard())


async def binance_admin_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Make one read-only call to Binance and report whether it worked."""
    query = update.callback_query
    if await _deny(update):
        return

    if not bp.binance_configured():
        missing = ", ".join(_missing_credentials())
        await query.answer(f"Nothing to test - the server is missing {missing}.",
                           show_alert=True)
        return

    await query.answer()
    await query.edit_message_text("🧪 Testing Binance configuration…")

    service = get_service()
    ok, detail = await asyncio.to_thread(service.test_configuration)

    # test_configuration() returns a description of the failure, never the
    # credentials or the raw response body.
    head = "✅ CONFIGURATION OK" if ok else "❌ CONFIGURATION FAILED"
    body = (f"{head}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{detail}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━")

    def _log_sync():
        with get_db_session() as session:
            log_admin_action(session, update.effective_user.id, "binance_test_config",
                             target_type="settings",
                             details="ok" if ok else "failed")
            session.commit()

    await asyncio.to_thread(_log_sync)

    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")],
    ]))


# ======================================================================
# Monitoring screen
# ======================================================================

def _status_counts_sync() -> dict:
    """How many Binance transactions sit in each status.

    One grouped query, so the filter row can carry counts. Without them an
    admin has to open all six filters to find where anything is, and the
    default screen reads as broken whenever its own queue is empty.
    """
    with get_db_session() as session:
        rows = (session.query(Transaction.status, func.count(Transaction.id))
                .filter(Transaction.payment_method == PaymentMethod.BINANCE_PAY)
                .group_by(Transaction.status)
                .all())
    return {status: count for status, count in rows}


def _filter_keyboard(active_key: str, counts: dict):
    rows, row = [], []
    for key, label, status in _FILTERS:
        count = counts.get(status, 0)
        text = f"{label} ({count})" if count else label
        if key == active_key:
            text = f"• {text} •"
        row.append(InlineKeyboardButton(text, callback_data=f"binadmin_mon_{key}_0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


async def binance_admin_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List Binance transactions in one status."""
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    # binadmin_mon_<key>_<page>
    parts = query.data.split("_")
    try:
        key, page = parts[2], int(parts[3])
    except (IndexError, ValueError):
        key, page = "pending", 0
    if key not in _FILTER_BY_KEY:
        key = "pending"
    label, status = _FILTER_BY_KEY[key]

    def _sync():
        counts = _status_counts_sync()
        with get_db_session() as session:
            q = session.query(Transaction).filter(
                Transaction.payment_method == PaymentMethod.BINANCE_PAY,
                Transaction.status == status,
            ).order_by(Transaction.created_at.desc())

            total = q.count()
            rows = q.offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()
            return counts, total, [
                {
                    'id': t.id,
                    'telegram_id': t.user.telegram_id if t.user else None,
                    'amount': t.amount,
                    'provider_txn_id': t.provider_transaction_id,
                    'attempts': t.verification_attempts or 0,
                    'created_at': t.created_at,
                    'error': t.last_verification_error,
                }
                for t in rows
            ]

    counts, total, rows = await asyncio.to_thread(_sync)

    lines = [f"📊 BINANCE PAYMENTS - {label}",
             "━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    if not rows:
        lines.append("No transactions in this state.")
    else:
        for r in rows:
            when = r['created_at'].strftime('%Y-%m-%d %H:%M') if r['created_at'] else '-'
            lines.append(f"🆔 #{r['id']} · {r['amount']:.2f} {settings.BINANCE_PAY_CURRENCY}")
            lines.append(f"👤 {r['telegram_id']} · {when}")
            if r['provider_txn_id']:
                lines.append(f"🔢 {r['provider_txn_id']}")
            if r['attempts']:
                lines.append(f"🔁 attempts: {r['attempts']}")
            if r['error']:
                lines.append(f"⚠️ {r['error'][:80]}")
            lines.append("")
    lines += [f"Total: {total}", "━━━━━━━━━━━━━━━━━━━━━━━━"]

    keyboard = _filter_keyboard(key, counts)

    # A re-verify button only for the states where verifying again can
    # still change the outcome. Re-running it on a COMPLETED row would do
    # nothing (settlement re-checks status under a lock), but offering the
    # button implies it might, so it is not offered.
    if status in (TransactionStatus.PENDING, TransactionStatus.VERIFYING,
                  TransactionStatus.MANUAL_REVIEW, TransactionStatus.FAILED):
        for r in rows:
            if not r['provider_txn_id']:
                continue
            buttons = [InlineKeyboardButton(f"🔄 Re-verify #{r['id']}",
                                            callback_data=f"binadmin_retry_{r['id']}")]
            # Closing out a stuck payment only makes sense once the
            # automatic path has given up on it.
            if status == TransactionStatus.MANUAL_REVIEW:
                buttons.append(InlineKeyboardButton(
                    "🚫 Close", callback_data=f"binadmin_close_{r['id']}"))
            keyboard.append(buttons)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "⬅️ Prev", callback_data=f"binadmin_mon_{key}_{page - 1}"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            "➡️ Next", callback_data=f"binadmin_mon_{key}_{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")])

    try:
        await query.edit_message_text("\n".join(lines),
                                      reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        logger.debug("Binance monitoring screen not re-rendered", exc_info=True)


async def binance_admin_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-run verification for one transaction, on demand.

    Runs the same verify_transaction() as everything else, so an admin
    cannot produce a credit the automatic path would have refused.
    """
    query = update.callback_query
    if await _deny(update):
        return

    try:
        transaction_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.answer("Invalid request.", show_alert=True)
        return

    await query.answer()

    await query.edit_message_text(f"🔄 Re-verifying #{transaction_id}…")

    def _load_sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(
                id=transaction_id, payment_method=PaymentMethod.BINANCE_PAY
            ).first()
            if not txn:
                return None
            return (txn.user.telegram_id if txn.user else None,
                    txn.provider_transaction_id)

    loaded = await asyncio.to_thread(_load_sync)
    if loaded is None:
        await query.edit_message_text(
            "❌ Transaction not found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")]]))
        return

    user_telegram_id, provider_txn_id = loaded
    if not provider_txn_id:
        await query.edit_message_text(
            "❌ This transaction has no Binance ID submitted yet, so there "
            "is nothing to verify.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")]]))
        return

    # A manual re-verify should not be blocked by an exhausted budget - the
    # admin is the one asking - so reset the counter and let it run again.
    def _reopen_sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(
                id=transaction_id).with_for_update().first()
            if txn and txn.status in (TransactionStatus.MANUAL_REVIEW,
                                      TransactionStatus.FAILED):
                txn.status = TransactionStatus.PENDING
                txn.verification_attempts = 0
            log_admin_action(session, update.effective_user.id, "binance_retry_verify",
                             target_type="transaction", target_id=transaction_id)
            session.commit()

    await asyncio.to_thread(_reopen_sync)

    try:
        user_text, admin_text = await bp.verify_transaction(transaction_id)
    except Exception:
        logger.exception("Admin re-verify failed for transaction %s", transaction_id)
        await query.edit_message_text(
            "❌ Verification could not be completed. Check the logs.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")]]))
        return

    settled = user_text.startswith("✅") or user_text.startswith("❌")
    if settled and user_telegram_id:
        try:
            await context.bot.send_message(chat_id=user_telegram_id, text=user_text)
        except Exception:
            pass

    if admin_text:
        await notify_admin(context, admin_text)

    await query.edit_message_text(
        f"Result for #{transaction_id}:\n\n{user_text}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Payment Monitoring",
                                  callback_data="binadmin_mon_pending_0")],
            [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")],
        ]))


async def binance_admin_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close a manual-review transaction without crediting it.

    For the case where an admin has checked Binance by hand and the payment
    genuinely never arrived. It only ever moves a row to FAILED - crediting
    is not reachable from here.
    """
    query = update.callback_query
    if await _deny(update):
        return

    try:
        transaction_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.answer("Invalid request.", show_alert=True)
        return

    await query.answer()

    def _sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(
                id=transaction_id, payment_method=PaymentMethod.BINANCE_PAY
            ).with_for_update().first()
            if not txn:
                return "not_found"
            if txn.status == TransactionStatus.COMPLETED:
                return "completed"
            txn.status = TransactionStatus.FAILED
            txn.last_verification_at = datetime.utcnow()
            log_admin_action(session, update.effective_user.id, "binance_close_unpaid",
                             target_type="transaction", target_id=transaction_id)
            session.commit()
            return "closed"

    state = await asyncio.to_thread(_sync)

    text = {
        "not_found": "❌ Transaction not found.",
        "completed": "ℹ️ Already completed - left untouched.",
        "closed": f"❌ #{transaction_id} closed as unpaid. No balance was credited.",
    }[state]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Payment Monitoring",
                              callback_data="binadmin_mon_pending_0")],
        [InlineKeyboardButton("🔙 Back", callback_data="binadmin_menu")],
    ]))
