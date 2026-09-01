"""Demo/test provider for Binance Pay.

Used when BINANCE_TEST_MODE=true, and by the test suite. It implements the
same surface as BinancePayService so nothing else in the codebase needs to
know which one it is talking to.

Two safety properties matter here:

  * A mock SUCCESS can only ever be produced while BINANCE_TEST_MODE is on
    (see binance_pay.get_service), so it cannot credit a production wallet
    that is running against the real service.
  * The canned transactions are shaped exactly like the documented Binance
    response, so the verification rules being exercised are the real ones -
    the mock replaces the network, not the logic. Everything below goes
    through BinancePayService._check_matched_record, unchanged.

Which id produces which outcome:

    MOCK-SUCCESS-<amount>   pays exactly <amount> USDT       -> SUCCESS
    MOCK-SUCCESS            pays 10.00 USDT                  -> SUCCESS/mismatch
    MOCK-WRONG-AMOUNT       pays 0.01 USDT                   -> INVALID
    MOCK-WRONG-ASSET        pays in BUSD                     -> INVALID
    MOCK-REFUND             orderType=PAY_REFUND             -> INVALID
    MOCK-OUTGOING           negative amount (expenditure)    -> INVALID
    MOCK-OLD                transactionTime long in the past -> INVALID
    MOCK-UNKNOWN / anything not listed  -> not in history    -> TEMPORARY
    MOCK-TIMEOUT            simulates a network timeout      -> TEMPORARY
    MOCK-RATELIMIT          simulates HTTP 429               -> TEMPORARY
    MOCK-APIERROR           simulates success=false          -> TEMPORARY
    MOCK-AUTHFAIL           simulates 401                    -> TEMPORARY
"""

import logging
import time
from decimal import Decimal

from config.settings import settings
from services.binance_pay import (
    BinancePayService,
    BinanceAuthError,
    BinanceTemporaryError,
)

logger = logging.getLogger(__name__)

_DAY_MS = 24 * 60 * 60 * 1000


class MockBinancePayService(BinancePayService):
    """BinancePayService with the HTTP layer replaced by canned data."""

    def __init__(self, api_key: str = None, api_secret: str = None):
        # Deliberately does not read real credentials.
        self.api_key = api_key or "MOCK_KEY"
        self.api_secret = api_secret or "MOCK_SECRET"
        self._submitted_id = None

    @property
    def is_configured(self) -> bool:
        return True

    def verify_payment(self, provider_transaction_id, expected_amount,
                       expected_currency=None, not_before_ms=None):
        # Remember what was asked for so fetch_pay_transactions can build a
        # matching record; the rule-checking itself stays in the real class.
        self._submitted_id = (provider_transaction_id or "").strip()
        return super().verify_payment(
            provider_transaction_id, expected_amount,
            expected_currency=expected_currency, not_before_ms=not_before_ms,
        )

    def fetch_pay_transactions(self, start_time_ms: int = None, limit: int = 100) -> list:
        submitted = (self._submitted_id or "").strip()
        now_ms = int(time.time() * 1000)

        # Provider-level failures, before any record exists.
        if submitted == "MOCK-TIMEOUT":
            raise BinanceTemporaryError("network error: Timeout")
        if submitted == "MOCK-RATELIMIT":
            raise BinanceTemporaryError("rate limited by Binance")
        if submitted == "MOCK-APIERROR":
            raise BinanceTemporaryError("provider returned success=false (code 000001)")
        if submitted == "MOCK-AUTHFAIL":
            raise BinanceAuthError("authentication rejected (HTTP 401)")

        def record(**overrides):
            base = {
                "orderType": "PAY",
                "transactionId": submitted,
                "transactionTime": now_ms,
                "amount": "10.00",
                "currency": settings.BINANCE_PAY_CURRENCY,
                "walletType": 1,
                "fundsDetail": [],
                "payerInfo": {"name": "Demo Payer", "type": "USER", "binanceId": "123456789"},
                "receiverInfo": {"name": "Demo Shop", "type": "USER", "binanceId": "987654321"},
            }
            base.update(overrides)
            return [base]

        if submitted.startswith("MOCK-SUCCESS-"):
            # MOCK-SUCCESS-25.00 pays exactly 25.00, so a test can line the
            # amount up with whatever the local transaction asked for.
            raw = submitted[len("MOCK-SUCCESS-"):]
            try:
                amount = str(Decimal(raw))
            except Exception:
                amount = "10.00"
            return record(amount=amount)

        if submitted == "MOCK-SUCCESS":
            return record()
        if submitted == "MOCK-WRONG-AMOUNT":
            return record(amount="0.01")
        if submitted == "MOCK-WRONG-ASSET":
            return record(currency="BUSD")
        if submitted == "MOCK-REFUND":
            return record(orderType="PAY_REFUND")
        if submitted == "MOCK-OUTGOING":
            return record(amount="-10.00")
        if submitted == "MOCK-OLD":
            return record(transactionTime=now_ms - 30 * _DAY_MS)

        # Anything else: simply not in history.
        return []

    def test_configuration(self) -> tuple:
        return True, "test mode - no real Binance call was made"
