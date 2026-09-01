"""Webhook server for receiving CryptoBot payment notifications.

Runs inside the same process as the bot (see app.py), which means a confirmed
payment can be credited AND announced to the user immediately, instead of the
user having to open their wallet to notice the new balance.

It also serves ``/health``, which Railway uses as its healthcheck endpoint.

Setup:
1. Deploy on Railway and copy the public domain of the service.
2. Open @CryptoBot -> Crypto Pay -> My Apps -> your app -> Webhooks.
3. Enable webhooks and set the URL to
   https://<your-railway-domain>/webhook/cryptobot
"""

import hmac
import hashlib
import logging
from datetime import datetime

from flask import Flask, request, jsonify

from database.db import get_db_session
from database.models import Transaction, TransactionStatus, PaymentMethod, User
from config.settings import settings

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Callback installed by app.py: notify(telegram_id: int, text: str) -> None.
# Thread-safe; scheduled onto the bot's event loop.
_notifier = None


def set_notifier(fn):
    """Register the function used to push a Telegram message to a user."""
    global _notifier
    _notifier = fn


def _notify(telegram_id: int, text: str):
    if _notifier is None:
        logger.warning("No notifier registered; user %s was not messaged", telegram_id)
        return
    try:
        _notifier(telegram_id, text)
    except Exception:
        logger.exception("Failed to notify user %s", telegram_id)


def verify_signature(body: bytes, signature: str) -> bool:
    """Verify the CryptoBot webhook signature (HMAC-SHA256 over the raw body)."""
    if not settings.CRYPTO_BOT_API_KEY:
        # With no token every signature check would run against an empty and
        # therefore guessable secret.
        logger.error("CRYPTO_BOT_API_KEY is not configured - rejecting webhook")
        return False

    secret_key = hashlib.sha256(settings.CRYPTO_BOT_API_KEY.encode()).digest()
    calculated_signature = hmac.new(secret_key, body, hashlib.sha256).hexdigest()

    try:
        return hmac.compare_digest(calculated_signature, signature)
    except (TypeError, ValueError):
        return False


def process_invoice_paid(invoice_data: dict):
    """Credit a wallet from a paid-invoice notification."""
    if not isinstance(invoice_data, dict):
        logger.warning("Webhook payload is not an object, ignoring")
        return

    invoice_id = invoice_data.get('invoice_id')
    status = invoice_data.get('status')
    invoice_amount = invoice_data.get('amount')

    if status != 'paid':
        logger.info("Invoice %s is not paid (status=%s), ignoring", invoice_id, status)
        return

    notify = None

    try:
        with get_db_session() as session:
            # SQLAlchemy persists Enum members by NAME ("CRYPTO_WALLET"), so
            # filtering on the string 'crypto_wallet' matched nothing and every
            # webhook used to be silently dropped.
            candidates = session.query(Transaction.id, Transaction.crypto_address).filter(
                Transaction.payment_method == PaymentMethod.CRYPTO_WALLET,
                Transaction.status == TransactionStatus.PENDING
            ).all()

            transaction_id = None
            for txn_id, crypto_address in candidates:
                if crypto_address and crypto_address.startswith(f"{invoice_id}|"):
                    transaction_id = txn_id
                    break

            if transaction_id is None:
                # Normal when the 30s poller got there first.
                logger.info("No pending transaction for invoice %s", invoice_id)
                return

            # Re-fetch under a row lock and re-check status: the 30s poller
            # (check_pending_payments) runs in this same process and could be
            # crediting this exact transaction right now. Without the lock,
            # both paths could read status=PENDING and credit the wallet twice.
            transaction = session.query(Transaction).filter_by(
                id=transaction_id, status=TransactionStatus.PENDING
            ).with_for_update().first()

            if not transaction:
                logger.info("Transaction %s for invoice %s already processed", transaction_id, invoice_id)
                return

            # Do not credit more than the invoice was actually issued for.
            try:
                if invoice_amount is not None and float(invoice_amount) + 0.01 < float(transaction.amount):
                    logger.error("Amount mismatch for invoice %s: invoice=%s expected=%s",
                                 invoice_id, invoice_amount, transaction.amount)
                    return
            except (TypeError, ValueError):
                logger.error("Unparsable invoice amount for %s: %r", invoice_id, invoice_amount)
                return

            user = session.query(User).filter_by(id=transaction.user_id).first()
            if not user:
                logger.error("User missing for transaction %s", transaction.id)
                return

            transaction.status = TransactionStatus.COMPLETED
            transaction.completed_at = datetime.utcnow()
            user.wallet_balance = round(user.wallet_balance + transaction.amount, 2)

            notify = (user.telegram_id, transaction.amount, user.wallet_balance, transaction.id)
            logger.info("Payment credited via webhook: txn #%s, $%.2f",
                        transaction.id, transaction.amount)
    except Exception:
        logger.exception("Error processing webhook for invoice %s", invoice_id)
        return

    if notify:
        telegram_id, amount, new_balance, txn_id = notify
        _notify(
            telegram_id,
            f"✅ Payment Confirmed!\n\n"
            f"💰 Amount: ${amount:.2f}\n"
            f"🔄 Your new wallet balance: ${new_balance:.2f}\n\n"
            f"Thank you for your payment!"
        )
        if settings.ADMIN_TELEGRAM_ID:
            _notify(
                settings.ADMIN_TELEGRAM_ID,
                f"💰 New Payment Received\n\n"
                f"👤 User ID: {telegram_id}\n"
                f"💰 Amount: ${amount:.2f}\n"
                f"📝 Transaction ID: #{txn_id}\n"
                f"🔄 Payment Method: crypto_wallet"
            )


@app.route('/webhook/cryptobot', methods=['POST'])
def cryptobot_webhook():
    """Webhook endpoint for CryptoBot payment notifications."""
    try:
        signature = request.headers.get('crypto-pay-api-signature')
        if not signature:
            return jsonify({'error': 'No signature'}), 401

        body = request.get_data()
        if not verify_signature(body, signature):
            logger.warning("Rejected webhook with an invalid signature")
            return jsonify({'error': 'Invalid signature'}), 401

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid payload'}), 400

        if data.get('update_type') != 'invoice_paid':
            return jsonify({'ok': True}), 200

        process_invoice_paid(data.get('payload'))
        return jsonify({'ok': True}), 200

    except Exception:
        logger.exception("Webhook error")
        # Do not echo internal error details back to the caller.
        return jsonify({'error': 'internal error'}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Healthcheck endpoint used by Railway."""
    return jsonify({
        'status': 'ok',
        'service': 'telegram-store-bot',
        'timestamp': datetime.utcnow().isoformat()
    }), 200


@app.route('/', methods=['GET'])
def index():
    """Root endpoint. Deliberately does not expose any store or config data."""
    return jsonify({'status': 'ok', 'webhook': '/webhook/cryptobot'}), 200


def run_server(port: int = None):
    """Serve the app with waitress (Flask's dev server is not for production)."""
    from waitress import serve
    port = port or settings.PORT
    logger.info("Webhook/health server listening on 0.0.0.0:%s", port)
    serve(app, host='0.0.0.0', port=port, threads=4, _quiet=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_server()
