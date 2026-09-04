"""Binance Pay top-up flow.

Plugs into the EXISTING top-up system: the same Transaction rows, the same
wallet_balance, the same expiry job, the same admin manual-confirm screens.
Nothing here is a parallel payment stack.

Flow, matching the agreed UI exactly:

    Top Up -> amount -> [🟡 Binance Pay]
      -> Transaction(PENDING, payment_method=BINANCE_PAY, expires_at=...)
      -> checkout message
      -> [🧾 Submit Order ID]           (and [❌ Cancel Order])
      -> order-id input screen          (NO buttons at all)
      -> user sends the id
      -> that submission itself starts verification
      -> SUCCESS / INVALID / TEMPORARY

The wallet is credited in exactly one place: _settle_success_sync(), under
a row lock, after re-checking status. The database's UNIQUE (provider,
provider_transaction_id) is what makes a double credit impossible even if
two verifications race.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import (
    get_db_session, User, Transaction, TransactionStatus, PaymentMethod,
)
from config.settings import settings
from services.binance_pay import get_service, VerificationOutcome
from services import payment_methods
from utils import notify_admin, to_money

logger = logging.getLogger(__name__)

PROVIDER = "binance"

# Conversation state for the order-id input screen. Numbered clear of the
# top-up conversation's own AMOUNT/METHOD states, which it shares a
# ConversationHandler with.
BINANCE_ORDER_ID = 20

# A Binance transaction id is numeric and short; this only rejects obvious
# junk early so we do not spend a Weight-3000 API call on it.
_MAX_ID_LENGTH = 64

# Ceiling on how many rows one retry pass verifies. Each row costs a
# Weight(UID) 3000 call, so an unbounded backlog could burn the whole API
# quota in a single tick and lock the account out of verifying anything.
_RETRY_BATCH_SIZE = 10


# Cache of Settings.binance_pay_enabled, the admin kill switch.
#
# binance_pay_available() is called from keyboard builders that run on the
# event loop, so it must not touch the database - that is exactly the
# blocking-IO pattern the rest of this codebase moves into threads. The
# flag lives in one process, is written only by the admin toggle below,
# and is loaded once at startup, so a plain module global is enough.
# None means "no admin has decided", in which case BINANCE_PAY_ENABLED
# supplies the answer. Once an admin uses the switch, their choice wins:
# the environment variable is the starting position, not a veto that the
# panel can never overcome.
def _switch_is_on() -> bool:
    """The live on/off state: the admin's choice, else the environment."""
    return payment_methods.is_on(PROVIDER)


def _write_admin_toggle_sync(enabled: bool, admin_telegram_id: int) -> bool:
    """Persist the switch and update the cache. Call from a thread."""
    return payment_methods.set_switch_sync(PROVIDER, enabled, admin_telegram_id)


def refresh_admin_toggle():
    """Load the switch at startup, before any update is served."""
    return payment_methods.refresh().get(PROVIDER)


def binance_configured() -> bool:
    """Whether the server holds credentials that could verify a payment.

    Deliberately does NOT consider BINANCE_PAY_ENABLED. That variable says
    whether the method should be offered; this says whether it *could* be.
    Folding the two together is what made the admin panel's Enable button
    impossible to use: the panel refused to switch anything on while the
    environment variable was off, and the environment variable is not
    something the panel can change.
    """
    return payment_methods.configured(PROVIDER)


def binance_pay_available() -> bool:
    """Whether to offer Binance Pay at all.

    Hidden unless it is switched on AND actually usable, so a user can
    never reach a checkout whose payment could never be verified.
    """
    return payment_methods.available(PROVIDER)


def _remaining_time(expires_at) -> str:
    if not expires_at:
        return "N/A"
    delta = (expires_at - datetime.utcnow()).total_seconds()
    if delta <= 0:
        return "expired"
    minutes, seconds = divmod(int(delta), 60)
    return f"{minutes}m {seconds}s"


def _checkout_text(transaction_id: int, amount, expires_at) -> str:
    return f"""🟡 BINANCE PAY CHECKOUT
━━━━━━━━━━━━━━━━━━━━━━━━

🧾 TRANSACTION SUMMARY

💰 Amount Due: {amount} {settings.BINANCE_PAY_CURRENCY}
🆔 Order ID: #{transaction_id}
🔢 Binance Pay ID: {settings.BINANCE_PAY_ID}
⏳ Expires In: {_remaining_time(expires_at)}

━━━━━━━━━━━━━━━━━━━━━━━━

📌 PAYMENT INSTRUCTIONS

1️⃣ Open Binance App → Pay → Send
2️⃣ Enter Binance Pay ID: {settings.BINANCE_PAY_ID}
3️⃣ Send exactly {amount} {settings.BINANCE_PAY_CURRENCY}
4️⃣ Submit your Order ID below

⚠️ Exact amount is required for
automated payment verification.

━━━━━━━━━━━━━━━━━━━━━━━━"""


def _checkout_keyboard(transaction_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Submit Order ID", callback_data=f"binance_submit_{transaction_id}")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"binance_cancel_{transaction_id}")],
    ])


# ======================================================================
# 1. Checkout
# ======================================================================

async def payment_method_binance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create the local Transaction and show the checkout."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    if not binance_pay_available():
        await query.edit_message_text(
            "❌ Binance Pay is not available right now.\n\n"
            "Please choose another payment method or contact support."
        )
        return ConversationHandler.END

    if usd_amount <= 0:
        await query.edit_message_text("❌ Invalid amount. Please start the top-up again.")
        return ConversationHandler.END

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                return None

            # The existing Transaction model and the existing expiry
            # window - check_expired_payments already sweeps these.
            from utils import calculate_expiry_time
            transaction = Transaction(
                user_id=user.id,
                amount=usd_amount,
                payment_method=PaymentMethod.BINANCE_PAY,
                status=TransactionStatus.PENDING,
                provider=PROVIDER,
                expires_at=calculate_expiry_time(settings.PAYMENT_EXPIRY_HOURS),
            )
            session.add(transaction)
            session.commit()
            session.refresh(transaction)
            return transaction.id, transaction.amount, transaction.expires_at

    result = await asyncio.to_thread(_sync)
    if result is None:
        await query.edit_message_text("❌ User not found.")
        return ConversationHandler.END

    transaction_id, amount, expires_at = result
    context.user_data['binance_transaction_id'] = transaction_id

    await query.edit_message_text(
        _checkout_text(transaction_id, f"{amount:.2f}", expires_at),
        reply_markup=_checkout_keyboard(transaction_id),
    )
    return ConversationHandler.END


# ======================================================================
# 2. Submit Order ID  ->  input screen (no buttons)
# ======================================================================

async def binance_submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the order-id input state.

    Deliberately renders NO keyboard: no second "Verify Payment" button and
    no "Cancel Order" button on this screen. Sending the id is the action.
    """
    query = update.callback_query
    await query.answer()

    try:
        transaction_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return ConversationHandler.END

    context.user_data['binance_transaction_id'] = transaction_id

    await query.edit_message_text(
        "🧾 ENTER BINANCE ORDER ID\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please enter your Binance\n"
        "Transaction/Order ID below.\n\n"
        "💡 Example:\n"
        "1234567890123456789\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return BINANCE_ORDER_ID


# ======================================================================
# 3. The submission itself triggers verification
# ======================================================================

async def binance_order_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the provider id and verify immediately - no extra button."""
    submitted = (update.message.text or "").strip()
    transaction_id = context.user_data.get('binance_transaction_id')
    telegram_id = update.effective_user.id

    if not transaction_id:
        await update.message.reply_text("❌ Session expired. Please start the top-up again.")
        return ConversationHandler.END

    if not submitted or len(submitted) > _MAX_ID_LENGTH:
        await update.message.reply_text(
            "❌ That does not look like a Binance Transaction/Order ID.\n\n"
            "💡 Example:\n1234567890123456789\n\n"
            "Please try again."
        )
        return BINANCE_ORDER_ID

    # Claim the transaction: PENDING -> VERIFYING under a row lock. This is
    # what stops a double click, and a user racing the background worker,
    # from both running a verification for the same row.
    def _claim_sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(
                id=transaction_id, payment_method=PaymentMethod.BINANCE_PAY
            ).with_for_update().first()

            if not txn:
                return "not_found", None
            if txn.user.telegram_id != telegram_id:
                # Not your order.
                return "not_found", None
            if txn.status == TransactionStatus.COMPLETED:
                return "already_completed", None
            if txn.status == TransactionStatus.VERIFYING:
                return "in_progress", None
            if txn.status == TransactionStatus.EXPIRED:
                return "expired", None
            if txn.status not in (TransactionStatus.PENDING, TransactionStatus.FAILED):
                return "not_pending", None
            if txn.expires_at and datetime.utcnow() > txn.expires_at:
                txn.status = TransactionStatus.EXPIRED
                return "expired", None

            # Reject an id already attached to another local transaction
            # before the UNIQUE constraint has to.
            taken = session.query(Transaction.id).filter(
                Transaction.provider == PROVIDER,
                Transaction.provider_transaction_id == submitted,
                Transaction.id != txn.id,
            ).first()
            if taken:
                return "already_used", None

            txn.provider = PROVIDER
            txn.provider_transaction_id = submitted
            txn.status = TransactionStatus.VERIFYING
            session.commit()
            return "claimed", (txn.amount, txn.created_at)

    try:
        state, payload = await asyncio.to_thread(_claim_sync)
    except Exception:
        # Most likely the UNIQUE constraint: the id belongs elsewhere.
        logger.exception("Could not attach Binance id to transaction %s", transaction_id)
        await update.message.reply_text(_failed_text())
        return ConversationHandler.END

    if state == "not_found":
        await update.message.reply_text("❌ Order not found.")
        return ConversationHandler.END
    if state == "already_completed":
        await update.message.reply_text("ℹ️ PAYMENT ALREADY PROCESSED")
        return ConversationHandler.END
    if state == "in_progress":
        await update.message.reply_text("🔄 VERIFYING PAYMENT\n\nA verification is already running.")
        return ConversationHandler.END
    if state == "expired":
        await update.message.reply_text("⌛ This order has expired. Please start a new top-up.")
        return ConversationHandler.END
    if state == "already_used":
        await update.message.reply_text(_failed_text())
        return ConversationHandler.END
    if state != "claimed":
        await update.message.reply_text(_failed_text())
        return ConversationHandler.END

    await update.message.reply_text(
        "🔄 VERIFYING PAYMENT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Please wait while we verify\n"
        "your payment...\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    outcome_text, notify = await verify_transaction(transaction_id)
    await update.message.reply_text(outcome_text)

    if notify:
        await notify_admin(context, notify)

    context.user_data.pop('binance_transaction_id', None)
    return ConversationHandler.END


# ======================================================================
# Verification + settlement (shared by the user path and the retry job)
# ======================================================================

def _failed_text() -> str:
    return ("❌ PAYMENT VERIFICATION FAILED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "We could not verify this payment.\n\n"
            "Possible reasons:\n"
            "• Invalid Order ID\n"
            "• Amount mismatch\n"
            "• Asset mismatch\n"
            "• Payment not completed\n"
            "• Order expired\n"
            "• Payment already processed\n\n"
            "💰 Your balance has NOT been credited.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━")


def _pending_text() -> str:
    return ("⏳ PAYMENT VERIFICATION PENDING\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "We could not verify the payment\n"
            "right now.\n\n"
            "💰 Your balance has NOT been credited.\n\n"
            "The system will automatically retry\n"
            "verification.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━")


def _success_text(amount, transaction_id) -> str:
    return ("✅ PAYMENT VERIFIED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Amount Added: {amount} {settings.BINANCE_PAY_CURRENCY}\n"
            f"🆔 Order ID: #{transaction_id}\n\n"
            "Your payment has been verified\n"
            "and your balance has been updated.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━")


async def verify_transaction(transaction_id: int):
    """Verify one local transaction. Returns (user_message, admin_message).

    Used by both the user's submission and the background retry job, so
    there is exactly one implementation of the settlement rules.
    """
    def _load_sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(id=transaction_id).first()
            if not txn:
                return None
            return (txn.amount, txn.provider_transaction_id, txn.created_at,
                    txn.expires_at, txn.status, txn.user.telegram_id)

    loaded = await asyncio.to_thread(_load_sync)
    if loaded is None:
        return _failed_text(), None

    amount, provider_txn_id, created_at, expires_at, status, user_telegram_id = loaded

    if status == TransactionStatus.COMPLETED:
        return "ℹ️ PAYMENT ALREADY PROCESSED", None

    not_before_ms = int(created_at.timestamp() * 1000) if created_at else None

    service = get_service()
    result = await asyncio.to_thread(
        service.verify_payment, provider_txn_id, amount,
        settings.BINANCE_PAY_CURRENCY, not_before_ms,
    )

    if result.outcome is VerificationOutcome.SUCCESS:
        settled = await asyncio.to_thread(_settle_success_sync, transaction_id)
        if settled == "already":
            return "ℹ️ PAYMENT ALREADY PROCESSED", None
        if settled != "ok":
            return _failed_text(), None

        admin_msg = ("🟢 BINANCE PAYMENT COMPLETED\n\n"
                     f"👤 User ID: {user_telegram_id}\n"
                     f"💰 Amount: {amount} {settings.BINANCE_PAY_CURRENCY}\n"
                     f"📝 Order ID: #{transaction_id}\n"
                     f"🔢 Binance Transaction ID: {provider_txn_id}")
        return _success_text(f"{amount:.2f}", transaction_id), admin_msg

    if result.outcome is VerificationOutcome.INVALID:
        await asyncio.to_thread(_record_failure_sync, transaction_id,
                                result.reason, TransactionStatus.FAILED)
        return _failed_text(), None

    # TEMPORARY_ERROR: stays pending for the retry job, never credits.
    exhausted = await asyncio.to_thread(_record_failure_sync, transaction_id,
                                        result.reason, TransactionStatus.PENDING)
    if exhausted:
        admin_msg = ("🟡 BINANCE PAYMENT REQUIRES REVIEW\n\n"
                     f"👤 User ID: {user_telegram_id}\n"
                     f"💰 Amount: {amount} {settings.BINANCE_PAY_CURRENCY}\n"
                     f"📝 Order ID: #{transaction_id}\n"
                     f"🔢 Binance ID: {provider_txn_id}\n"
                     f"⚠️ Reason: {result.reason}")
        return _pending_text(), admin_msg
    return _pending_text(), None


def _settle_success_sync(transaction_id: int) -> str:
    """Credit the wallet. The ONLY place a Binance payment adds balance.

    Re-fetches under a row lock and re-checks status, so a verification
    that was already settled by the other path (user vs background worker)
    cannot credit a second time.
    """
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(
            id=transaction_id
        ).with_for_update().first()

        if not txn:
            return "missing"
        if txn.status == TransactionStatus.COMPLETED:
            return "already"

        user = session.query(User).filter_by(id=txn.user_id).with_for_update().first()
        if not user:
            return "missing"

        txn.status = TransactionStatus.COMPLETED
        txn.completed_at = datetime.utcnow()
        txn.last_verification_at = datetime.utcnow()
        txn.last_verification_error = None
        user.wallet_balance = to_money(user.wallet_balance + txn.amount)
        session.commit()
        return "ok"


def _record_failure_sync(transaction_id: int, reason: str, new_status) -> bool:
    """Store the attempt. Returns True if the retry budget just ran out.

    On exhaustion the transaction goes to MANUAL_REVIEW - a terminal state
    that never credits by itself.
    """
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(
            id=transaction_id
        ).with_for_update().first()
        if not txn or txn.status == TransactionStatus.COMPLETED:
            return False

        txn.verification_attempts = (txn.verification_attempts or 0) + 1
        txn.last_verification_at = datetime.utcnow()
        txn.last_verification_error = (reason or "")[:500]

        exhausted = False
        if new_status == TransactionStatus.PENDING:
            if txn.verification_attempts >= settings.BINANCE_MAX_VERIFY_ATTEMPTS:
                txn.status = TransactionStatus.MANUAL_REVIEW
                exhausted = True
            else:
                txn.status = TransactionStatus.PENDING
        else:
            txn.status = new_status

        session.commit()
        return exhausted


# ======================================================================
# Background retry
# ======================================================================

async def retry_pending_binance_payments(context: ContextTypes.DEFAULT_TYPE):
    """Re-verify Binance top-ups whose id did not resolve on first try.

    Runs on the same job_queue as the CryptoBot poller - there is no second
    scheduler. It only ever picks up rows a user already submitted an id
    for, and it settles through the same verify_transaction() the user path
    uses, so the credit rules exist in exactly one place.

    The Pay history endpoint is Weight(UID) 3000, so this is spaced by
    BINANCE_VERIFY_RETRY_INTERVAL rather than the CryptoBot poll interval,
    and a row is skipped until that long has passed since its last attempt.
    """
    if not binance_pay_available():
        return

    def _due_sync():
        cutoff = datetime.utcnow() - timedelta(
            seconds=settings.BINANCE_VERIFY_RETRY_INTERVAL
        )
        with get_db_session() as session:
            rows = session.query(Transaction).filter(
                Transaction.payment_method == PaymentMethod.BINANCE_PAY,
                Transaction.status == TransactionStatus.PENDING,
                Transaction.provider_transaction_id.isnot(None),
                Transaction.verification_attempts < settings.BINANCE_MAX_VERIFY_ATTEMPTS,
            ).order_by(Transaction.created_at).limit(_RETRY_BATCH_SIZE).all()

            return [
                (t.id, t.user.telegram_id)
                for t in rows
                if t.last_verification_at is None or t.last_verification_at <= cutoff
            ]

    try:
        due = await asyncio.to_thread(_due_sync)
    except Exception:
        logger.exception("Binance retry job could not load pending transactions")
        return

    for transaction_id, user_telegram_id in due:
        try:
            user_text, admin_text = await verify_transaction(transaction_id)
        except Exception:
            logger.exception("Binance retry failed for transaction %s", transaction_id)
            continue

        # Only tell the user something actually changed. A retry that is
        # still waiting stays silent, otherwise a slow payment would spam
        # the user with an identical "still pending" message every few
        # minutes for the whole retry budget.
        settled = user_text.startswith("✅") or user_text.startswith("❌")
        if settled:
            try:
                await context.bot.send_message(chat_id=user_telegram_id, text=user_text)
            except Exception:
                pass  # user may have blocked the bot

        if admin_text:
            await notify_admin(context, admin_text)


# ======================================================================
# Cancel
# ======================================================================

async def binance_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a Binance checkout from the checkout screen."""
    query = update.callback_query
    await query.answer()

    try:
        transaction_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return ConversationHandler.END

    telegram_id = update.effective_user.id

    def _sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(
                id=transaction_id, payment_method=PaymentMethod.BINANCE_PAY
            ).with_for_update().first()
            if not txn or txn.user.telegram_id != telegram_id:
                return "not_found"
            if txn.status == TransactionStatus.COMPLETED:
                return "already_completed"
            if txn.status in (TransactionStatus.PENDING, TransactionStatus.VERIFYING):
                txn.status = TransactionStatus.FAILED
                session.commit()
            return "cancelled"

    state = await asyncio.to_thread(_sync)

    from utils import create_main_menu_keyboard
    if state == "already_completed":
        await query.edit_message_text("ℹ️ PAYMENT ALREADY PROCESSED",
                                      reply_markup=create_main_menu_keyboard())
    elif state == "not_found":
        await query.edit_message_text("❌ Order not found.",
                                      reply_markup=create_main_menu_keyboard())
    else:
        await query.edit_message_text("❌ Order cancelled. You can start a new top-up anytime.",
                                      reply_markup=create_main_menu_keyboard())

    context.user_data.pop('binance_transaction_id', None)
    return ConversationHandler.END
