"""Crypto Bot API service for cryptocurrency payments."""

import logging

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


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

            response = requests.post(
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
                invoice_hash = result.get("hash", "")      # Hash for URLs
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

        except Exception as e:
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

            # Format 1: "invoice_id|pay_url" (NEW FORMAT - numeric invoice_id)
            if "|" in crypto_address:
                invoice_id = crypto_address.split("|")[0]
                pass
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

            response = requests.get(
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
                        try:
                            if invoice_amount is not None and float(invoice_amount) + 0.01 < float(expected_amount):
                                logger.error(
                                    "Invoice %s amount mismatch: invoice=%s expected=%s - not crediting",
                                    invoice_id, invoice_amount, expected_amount
                                )
                                return False
                        except (TypeError, ValueError):
                            logger.error("Invoice %s has an unparsable amount: %r", invoice_id, invoice_amount)
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
