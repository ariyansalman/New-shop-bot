"""Crypto Bot API service for cryptocurrency payments."""

import logging
import time
from decimal import Decimal

import requests

from config.settings import settings
from utils.money import money_or_none

logger = logging.getLogger(__name__)

# Retried once a transient failure happens: connect/read timeouts, 5xx
# (the server didn't durably process the request) and 429 (rate limited).
# NOT retried: 4xx other than 429 - that's a bad request on our end and
# won't succeed on a second try.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.5)  # delay before attempt 2, then attempt 3


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """requests.request() with retry-with-backoff on transient failures.

    Only used for GET (check_payment_status) and the one POST
    (generate_payment_address / createInvoice). A retried POST after a
    timeout carries a small risk of creating a second invoice if the first
    request actually reached CryptoBot before the client-side timeout - the
    CryptoBot API has no idempotency-key support to close that gap. The
    downside is bounded, though: an orphaned extra invoice, never linked to
    a Transaction (only the last response's pay_url gets stored), which
    just sits unused until it expires. That's a better trade-off than the
    previous behavior of never retrying at all and just failing the top-up.
    """
    last_exc = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                logger.warning("CryptoBot API %s %s failed (%s), retrying...", method, url, exc)
                time.sleep(_BACKOFF_SECONDS[attempt])
                continue
            raise
        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS - 1:
            logger.warning("CryptoBot API %s %s returned %s, retrying...",
                            method, url, response.status_code)
            time.sleep(_BACKOFF_SECONDS[attempt])
            continue
        return response
    # Unreachable in practice (the loop always returns or raises), but keeps
    # the type checker and any future refactor honest.
    raise last_exc


class CryptoBotService:
    """Service for integrating with Crypto Bot API for cryptocurrency payments."""

    def __init__(self):
        """Initialize Crypto Bot service with API key."""
        self.api_key = settings.CRYPTO_BOT_API_KEY
        self.base_url = "https://pay.crypt.bot/api"

    def generate_payment_address(self, amount: float, transaction_id: int, crypto_currency: str = None, crypto_network: str = None) -> str:
        """Generate a unique payment invoice that accepts any cryptocurrency.

        Args:
            amount: Amount in USD
            transaction_id: Transaction ID for reference
            crypto_currency: Deprecated - kept for backwards compatibility
            crypto_network: Deprecated - kept for backwards compatibility

        Returns:
            String format: "invoice_id|pay_url" or None if failed
        """
        if not self.api_key:
            # Previously this returned a fake "sample" invoice URL, so users were
            # shown a payment button that could never be paid or verified.
            logger.error("CRYPTO_BOT_API_KEY is not configured - refusing to create an invoice")
            return None

        try:
            headers = {
                "Crypto-Pay-API-Token": self.api_key
            }

            # Create invoice in USD that accepts ANY cryptocurrency
            # User can choose which crypto to pay with on CryptoBot payment page
            payload = {
                "currency_type": "fiat",
                "fiat": "USD",
                "amount": str(amount),
                "description": f"Wallet top-up #{transaction_id}",
                "allow_comments": False,
                "allow_anonymous": False,
                "expires_in": int(settings.PAYMENT_EXPIRY_HOURS * 3600),
            }

            # Only send the "return to bot" button when a real bot username is
            # configured; the old hardcoded https://t.me/your_bot was a placeholder.
            if settings.BOT_USERNAME:
                payload["paid_btn_name"] = "callback"
                payload["paid_btn_url"] = (
                    f"https://t.me/{settings.BOT_USERNAME}?start=payment_{transaction_id}"
                )

            response = _request_with_retry(
                "POST",
                f"{self.base_url}/createInvoice",
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                logger.debug("CryptoBot createInvoice response: %s", data)

                if not data.get("ok"):
                    logger.error("CryptoBot API returned ok=false: %s", data)
                    return None

                result = data.get("result", {})
                # Get invoice ID and payment URL
                invoice_id = result.get("invoice_id", "")  # Numeric ID for API calls
                bot_invoice_url = result.get("bot_invoice_url", "")
                mini_app_url = result.get("mini_app_invoice_url", "")
                pay_url = bot_invoice_url or mini_app_url

                logger.info("Created invoice %s", invoice_id)

                # Store format: "invoice_id|pay_url" for later verification
                # We need the invoice_id for API calls, and pay_url for user payment
                if invoice_id and pay_url:
                    return f"{invoice_id}|{pay_url}"
                else:
                    logger.error("Missing invoice_id or pay_url in CryptoBot response")
                    return None
            else:
                logger.error("CryptoBot API error: %s", response.status_code)
                return None

        except Exception:
            logger.exception("Error generating crypto payment invoice")
            return None

    def check_payment_status(self, crypto_address: str, expected_amount: float) -> bool:
        """Check if payment has been received for the invoice.

        NOTE: This polling-based approach is a FALLBACK mechanism.
        For REAL-TIME payment notifications, use webhooks instead:
        - Run webhook_server.py alongside the bot
        - Configure webhook URL in @CryptoBot → Crypto Pay → My Apps → Webhooks
        - Webhooks provide instant payment confirmation vs. 30-second polling delay

        See webhook_server.py for implementation details.
        """
        if not self.api_key:
            logger.error("CRYPTO_BOT_API_KEY is not configured - cannot verify payments")
            return False

        try:
            # Extract invoice_id from stored format
            invoice_id = None

            # A PENDING transaction can legitimately have no address yet: the
            # row is committed before the createInvoice call, so a crash in
            # between leaves one behind. Without this guard the `in` test
            # below raises TypeError on None and the poller logged a fresh
            # traceback for that row every PAYMENT_CHECK_INTERVAL seconds.
            if not crypto_address:
                logger.debug("Transaction has no invoice address yet - nothing to check")
                return False

            # Format 1: "invoice_id|pay_url" (NEW FORMAT - numeric invoice_id)
            if "|" in crypto_address:
                invoice_id = crypto_address.split("|")[0]
            # Format 2: Old format - just URL (can't verify these, need to be manually confirmed)
            elif "https://t.me/CryptoBot" in crypto_address or "?start=" in crypto_address:
                logger.warning("Old URL-only invoice format; admin must confirm manually")
                return False
            else:
                invoice_id = crypto_address

            # Check if it's a sample address (no API key configured)
            if not invoice_id or "SAMPLE_" in str(invoice_id):
                logger.warning("Skipping payment check for invalid invoice id")
                return False

            headers = {
                "Crypto-Pay-API-Token": self.api_key,
                "Content-Type": "application/json"
            }

            # Get invoice details - CryptoBot API format
            # invoice_ids should be a comma-separated string of numeric IDs
            params = {}
            if invoice_id:
                # Ensure invoice_id is just the numeric ID
                params["invoice_ids"] = str(invoice_id).strip()

            response = _request_with_retry(
                "GET",
                f"{self.base_url}/getInvoices",
                headers=headers,
                params=params,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                logger.debug("CryptoBot getInvoices response: %s", data)

                items = data.get("result", {}).get("items", [])

                if items:
                    invoice = items[0]
                    status = invoice.get("status")
                    paid_at = invoice.get("paid_at")
                    paid_amount = invoice.get("paid_amount")
                    paid_asset = invoice.get("paid_asset")

                    logger.debug(
                        "Invoice %s: status=%s paid_at=%s paid_amount=%s %s",
                        invoice_id, status, paid_at, paid_amount, paid_asset
                    )

                    # CryptoBot invoice statuses: active, paid, expired
                    if status == "paid" or paid_at:
                        # Verify the invoiced amount still matches what we are
                        # about to credit. Without this check the wallet was
                        # credited with `expected_amount` no matter what the
                        # invoice actually said.
                        invoice_amount = invoice.get("amount")
                        if invoice_amount is not None:
                            # money_or_none goes through Decimal(str(x)), not
                            # float(), so the comparison isn't affected by
                            # binary-float noise in the JSON-decoded amount.
                            invoice_amount_dec = money_or_none(invoice_amount)
                            expected_amount_dec = money_or_none(expected_amount)
                            if invoice_amount_dec is None or expected_amount_dec is None:
                                logger.error("Invoice %s has an unparsable amount: %r", invoice_id, invoice_amount)
                                return False
                            if invoice_amount_dec + Decimal("0.01") < expected_amount_dec:
                                logger.error(
                                    "Invoice %s amount mismatch: invoice=%s expected=%s - not crediting",
                                    invoice_id, invoice_amount, expected_amount
                                )
                                return False

                        logger.info("Invoice %s is paid (status=%s)", invoice_id, status)
                        return True

                    logger.debug("Invoice %s still pending (status=%s)", invoice_id, status)
                    return False
                else:
                    logger.warning("No invoice found with id %s", invoice_id)
                    return False
            else:
                logger.error("CryptoBot API error checking status: %s", response.status_code)
                return False

        except Exception:
            logger.exception("Error checking crypto payment status")
            return False
