"""Binance Pay payment verification.

WHAT THIS USES, AND WHY
-----------------------
Binance has two different things with "Pay" in the name:

  1. The Binance Pay **Merchant** API (/binancepay/openapi/...), which needs
     a merchant account and credentials issued by the Merchant Admin Portal
     (certificate serial number and friends). This project does not use it.

  2. **Pay transaction history on a normal account**:
     GET https://api.binance.com/sapi/v1/pay/transactions
     A standard signed SAPI endpoint - the same X-MBX-APIKEY + HMAC-SHA256
     scheme as every other signed Binance endpoint, with an ordinary
     account API key/secret. That is what this module uses, and it is what
     makes "user pays, submits the transaction id, we verify it" work
     without a merchant account.

Documented shape of one record in the `data` array (nothing here is
invented - every field below is one Binance documents on that endpoint):

    orderType        PAY | PAY_REFUND | C2C | CRYPTO_BOX | CRYPTO_BOX_RF |
                     C2C_HOLDING | C2C_HOLDING_RF | PAYOUT | REMITTANCE
    transactionId    the id the payer can read in their Binance app
    transactionTime  epoch milliseconds
    amount           "Positive means income; negative means expenditure"
    currency         the asset, e.g. USDT
    walletType/walletTypes, fundsDetail
    payerInfo        { name, type, binanceId }
    receiverInfo     { name, type, email, binanceId, accountId, ... }

Envelope: { code, message, data: [...], success }

THREE LIMITATIONS, STATED PLAINLY
---------------------------------
1. There is no "fetch one transaction by id" endpoint. The only filters are
   startTime / endTime / limit, so verification pages recent history and
   matches locally. That is why _MAX_LOOKBACK_MS exists.

2. There is **no status field** on a record. The documented success signal
   is that the transaction appears in this account's Pay history at all,
   with a positive (income) amount. This module therefore checks presence +
   sign, and does not pretend to read a status Binance does not return.

3. Destination is implicit: this is our own account's history, fetched with
   our own key, so anything in it was received by us. receiverInfo is
   carried through for the audit trail rather than used as the check.

The endpoint is Weight(UID) 3000 - expensive. Callers must not poll it
tightly; see BINANCE_VERIFY_RETRY_INTERVAL.
"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional
from urllib.parse import urlencode

import requests

from config.settings import settings
from utils.money import money_or_none

logger = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com"
PAY_TRANSACTIONS_PATH = "/sapi/v1/pay/transactions"

# Only these order types are an incoming payment from a person. Refunds and
# payouts also show up in the same history and must never settle a top-up.
_ACCEPTED_ORDER_TYPES = frozenset({"PAY", "C2C"})

# How far back to look. Binance caps the window at 90 days; a top-up that
# old is long expired on our side anyway, so stay well inside it.
_MAX_LOOKBACK_MS = 3 * 24 * 60 * 60 * 1000  # 3 days

# Same retry policy shape as services/crypto_bot.py.
_RETRYABLE_STATUS_CODES = {429, 418, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.5)

_REQUEST_TIMEOUT = 15


class VerificationOutcome(Enum):
    """What the caller should do with a verification result.

    The distinction that matters: INVALID is a decision (this payment is
    not good, stop), TEMPORARY_ERROR is an absence of a decision (we could
    not reach Binance, ask again later). Only SUCCESS may credit a wallet.
    """
    SUCCESS = "success"
    INVALID = "invalid"
    TEMPORARY_ERROR = "temporary_error"


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    # Operator-facing, safe to store and show in the admin panel. Never
    # contains credentials or a raw provider payload.
    reason: str = ""
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    payer_binance_id: Optional[str] = None
    transaction_time_ms: Optional[int] = None
    # Small, non-secret extras kept for the audit trail.
    metadata: dict = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.outcome is VerificationOutcome.SUCCESS

    @property
    def is_temporary(self) -> bool:
        return self.outcome is VerificationOutcome.TEMPORARY_ERROR


def _sign(query_string: str, api_secret: str) -> str:
    """HMAC-SHA256 of the exact query string, hex encoded - Binance's
    documented signing scheme for signed SAPI endpoints."""
    return hmac.new(
        api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class BinancePayService:
    """Talks to Binance. Contains no Telegram and no database code.

    A caller that only wants to know "is this Binance transaction id a real
    payment of X USDT to us?" should use verify_payment(); everything else
    here supports that.
    """

    def __init__(self, api_key: str = None, api_secret: str = None):
        self.api_key = api_key if api_key is not None else settings.BINANCE_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.BINANCE_API_SECRET

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """requests.request() with backoff on transient failures.

        Same policy as services/crypto_bot.py, plus 418 (Binance returns it
        to a client that keeps hitting a rate limit after a 429).
        """
        last_exc = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = requests.request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    logger.warning("Binance %s failed (%s), retrying...", method, exc)
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_ATTEMPTS - 1:
                logger.warning("Binance returned %s, retrying...", response.status_code)
                time.sleep(_BACKOFF_SECONDS[attempt])
                continue
            return response
        raise last_exc

    def _signed_get(self, path: str, params: dict) -> requests.Response:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10_000
        query_string = urlencode(params)
        signature = _sign(query_string, self.api_secret)
        url = f"{BASE_URL}{path}?{query_string}&signature={signature}"
        return self._request_with_retry(
            "GET", url,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=_REQUEST_TIMEOUT,
        )

    def fetch_pay_transactions(self, start_time_ms: int = None, limit: int = 100) -> list:
        """Return the raw `data` array from GET /sapi/v1/pay/transactions.

        Raises BinanceTemporaryError for anything that might succeed later
        (network trouble, 5xx, rate limiting, a non-ok envelope) and
        BinanceAuthError for a credential/permission problem, which retrying
        will not fix.
        """
        if not self.is_configured:
            raise BinanceAuthError("Binance API credentials are not configured")

        if start_time_ms is None:
            start_time_ms = int(time.time() * 1000) - _MAX_LOOKBACK_MS

        try:
            response = self._signed_get(
                PAY_TRANSACTIONS_PATH,
                {"startTime": start_time_ms, "limit": limit},
            )
        except requests.exceptions.RequestException as exc:
            raise BinanceTemporaryError(f"network error: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            # Bad key, wrong permissions, or an IP that is not whitelisted.
            raise BinanceAuthError(f"authentication rejected (HTTP {response.status_code})")
        if response.status_code == 429 or response.status_code == 418:
            raise BinanceTemporaryError("rate limited by Binance")
        if response.status_code != 200:
            raise BinanceTemporaryError(f"unexpected HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceTemporaryError("response was not valid JSON") from exc

        # Documented envelope: {code, message, data, success}
        if payload.get("success") is False:
            raise BinanceTemporaryError(f"provider returned success=false (code {payload.get('code')})")

        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise BinanceTemporaryError("response data was not a list")
        return data

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_payment(self, provider_transaction_id: str, expected_amount: Decimal,
                       expected_currency: str = None,
                       not_before_ms: int = None) -> VerificationResult:
        """Check one user-submitted Binance transaction id against history.

        Args:
            provider_transaction_id: what the user typed in.
            expected_amount: the local transaction's amount.
            expected_currency: asset that must match, e.g. "USDT".
            not_before_ms: reject anything older than this (the local
                transaction's creation time). Stops someone claiming an
                unrelated older payment as their own top-up.
        """
        expected_currency = (expected_currency or settings.BINANCE_PAY_CURRENCY).upper()
        submitted = (provider_transaction_id or "").strip()

        if not submitted:
            return VerificationResult(VerificationOutcome.INVALID, "no transaction id submitted")

        try:
            records = self.fetch_pay_transactions(
                start_time_ms=not_before_ms - 60_000 if not_before_ms else None
            )
        except BinanceAuthError as exc:
            # Not the payer's fault and not fixed by retrying, but it is
            # also not proof the payment is bad - keep it out of INVALID so
            # nobody's payment gets rejected over our own misconfiguration.
            logger.error("Binance auth failure during verification: %s", exc)
            return VerificationResult(VerificationOutcome.TEMPORARY_ERROR, f"configuration problem: {exc}")
        except BinanceTemporaryError as exc:
            logger.warning("Binance temporarily unavailable: %s", exc)
            return VerificationResult(VerificationOutcome.TEMPORARY_ERROR, str(exc))

        match = None
        for record in records:
            if str(record.get("transactionId", "")).strip() == submitted:
                match = record
                break

        if match is None:
            # Genuinely ambiguous: an id that is simply wrong looks exactly
            # like one whose payment has not landed in history yet. Treat it
            # as temporary so the retry budget gets a chance; it converts to
            # MANUAL_REVIEW once that budget runs out, and never credits.
            return VerificationResult(
                VerificationOutcome.TEMPORARY_ERROR,
                "transaction id not found in recent Binance Pay history",
            )

        return self._check_matched_record(match, submitted, expected_amount,
                                          expected_currency, not_before_ms)

    def _check_matched_record(self, record: dict, submitted: str, expected_amount: Decimal,
                              expected_currency: str, not_before_ms: Optional[int]) -> VerificationResult:
        """Apply every rule we can actually evidence from the response."""
        order_type = str(record.get("orderType") or "").upper()
        currency = str(record.get("currency") or "").upper()
        amount = money_or_none(record.get("amount"))
        txn_time = record.get("transactionTime")
        payer = record.get("payerInfo") or {}
        receiver = record.get("receiverInfo") or {}

        metadata = {
            "orderType": order_type,
            "currency": currency,
            "walletType": record.get("walletType"),
            "payerBinanceId": payer.get("binanceId"),
            "payerType": payer.get("type"),
            "receiverBinanceId": receiver.get("binanceId"),
            "receiverType": receiver.get("type"),
        }

        def invalid(reason):
            return VerificationResult(VerificationOutcome.INVALID, reason,
                                      amount=amount, currency=currency,
                                      payer_binance_id=payer.get("binanceId"),
                                      transaction_time_ms=txn_time, metadata=metadata)

        # A refund or an outgoing payout is in the same history; it is not
        # somebody topping up.
        if order_type not in _ACCEPTED_ORDER_TYPES:
            return invalid(f"not an incoming payment (orderType={order_type or 'unknown'})")

        if amount is None:
            return invalid("provider amount was not a parsable number")

        # Documented: positive means income, negative means expenditure.
        # There is no status field to read - being here as income is the
        # completion signal.
        if amount <= 0:
            return invalid("amount is not an incoming payment")

        if currency != expected_currency:
            return invalid(f"asset mismatch (got {currency or 'unknown'}, expected {expected_currency})")

        # No partial payments, and no crediting more than was invoiced.
        if amount != expected_amount:
            return invalid(f"amount mismatch (got {amount}, expected {expected_amount})")

        if not_before_ms is not None and isinstance(txn_time, int) and txn_time < not_before_ms:
            return invalid("payment predates this order")

        return VerificationResult(
            VerificationOutcome.SUCCESS,
            "verified",
            amount=amount,
            currency=currency,
            payer_binance_id=payer.get("binanceId"),
            transaction_time_ms=txn_time,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Admin "Test Configuration"
    # ------------------------------------------------------------------

    def test_configuration(self) -> tuple:
        """Return (ok: bool, message: str) for the admin panel.

        Makes one read-only call. Moves no funds, and the message never
        contains any part of the credentials.
        """
        if not self.is_configured:
            return False, "API key/secret are not set"
        try:
            self.fetch_pay_transactions(limit=1)
        except BinanceAuthError as exc:
            return False, str(exc)
        except BinanceTemporaryError as exc:
            return False, f"could not reach Binance: {exc}"
        except Exception:
            logger.exception("Unexpected error while testing Binance configuration")
            return False, "unexpected error (see server logs)"
        return True, "credentials accepted by Binance"


class BinanceTemporaryError(Exception):
    """Something that might work on a later attempt."""


class BinanceAuthError(Exception):
    """Credentials/permissions/IP problem. Retrying will not help."""


def get_service():
    """Return the provider to verify with.

    In test mode this hands back the mock instead, so a demo environment
    can exercise every branch without real credentials - and, just as
    importantly, so a mock 'success' can never be produced by the real
    service against a real wallet.
    """
    if settings.BINANCE_TEST_MODE:
        from services.binance_pay_mock import MockBinancePayService
        return MockBinancePayService()
    return BinancePayService()
