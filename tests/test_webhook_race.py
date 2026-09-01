"""webhook_server.process_invoice_paid - regression test for the double-credit
race between the CryptoBot webhook and the 30s payment poller (both run in
the same process and could otherwise both read status=PENDING and credit
the same transaction twice).

process_invoice_paid() is a plain synchronous function, so this can test the
exact guard (re-fetch under a row lock, re-check status == PENDING) directly,
without needing to simulate real concurrency or mock python-telegram-bot.
SQLite makes with_for_update() a no-op, so this specifically tests the
status re-check, not row locking itself - real cross-process locking only
takes effect on Postgres. That's a known, documented limitation (see
AUDIT.md), not something a unit test can exercise without a Postgres
instance.
"""

from decimal import Decimal

from database import get_db_session, User, Transaction, TransactionStatus, PaymentMethod
import webhook_server

TELEGRAM_ID = 555002


def make_pending_transaction(invoice_id="999", amount="19.99", pay_url="https://t.me/x") -> int:
    with get_db_session() as session:
        user = User(telegram_id=TELEGRAM_ID, wallet_balance=Decimal("0.00"))
        session.add(user)
        session.flush()
        txn = Transaction(
            user_id=user.id, amount=Decimal(amount),
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING,
            crypto_address=f"{invoice_id}|{pay_url}",
        )
        session.add(txn)
        session.flush()
        return txn.id


def test_webhook_credits_wallet_once():
    txn_id = make_pending_transaction(invoice_id="1001", amount="19.99")

    webhook_server.process_invoice_paid({"invoice_id": "1001", "status": "paid", "amount": "19.99"})

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        user = session.query(User).filter_by(id=txn.user_id).first()
        assert txn.status == TransactionStatus.COMPLETED
        assert user.wallet_balance == Decimal("19.99")


def test_webhook_does_not_double_credit_a_duplicate_delivery():
    """Same webhook body delivered twice (CryptoBot retry, or network
    duplication) must credit the wallet exactly once.
    """
    txn_id = make_pending_transaction(invoice_id="1002", amount="25.00")
    payload = {"invoice_id": "1002", "status": "paid", "amount": "25.00"}

    webhook_server.process_invoice_paid(payload)
    webhook_server.process_invoice_paid(payload)  # duplicate delivery

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        user = session.query(User).filter_by(id=txn.user_id).first()
        assert user.wallet_balance == Decimal("25.00"), "must not be credited twice"


def test_webhook_skips_a_transaction_the_poller_already_completed():
    """The 30s poller (check_pending_payments) got to this transaction first
    and already completed it before the webhook call even starts here.

    Note: this exercises the outer status==PENDING filter that both the
    original code and the fix share, not specifically the inner
    with_for_update() re-check added by the fix (that one guards a narrower
    window - the gap *between* this function's two queries - which isn't
    reachable through this function's public behavior without truly
    concurrent execution; see the module docstring on why that's not
    something a SQLite-backed unit test can responsibly claim to prove).
    What this test does verify, unconditionally: a transaction that is not
    PENDING by the time process_invoice_paid looks at it is never credited.
    """
    txn_id = make_pending_transaction(invoice_id="1003", amount="10.00")

    # Simulate the poller having already processed this transaction.
    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        user = session.query(User).filter_by(id=txn.user_id).first()
        txn.status = TransactionStatus.COMPLETED
        user.wallet_balance = Decimal("10.00")

    webhook_server.process_invoice_paid({"invoice_id": "1003", "status": "paid", "amount": "10.00"})

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        user = session.query(User).filter_by(id=txn.user_id).first()
        assert user.wallet_balance == Decimal("10.00"), "must not be credited a second time"


def test_webhook_rejects_underpaid_invoice():
    txn_id = make_pending_transaction(invoice_id="1004", amount="100.00")

    # CryptoBot invoice paid for less than the pending transaction expects.
    webhook_server.process_invoice_paid({"invoice_id": "1004", "status": "paid", "amount": "1.00"})

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        user = session.query(User).filter_by(id=txn.user_id).first()
        assert txn.status == TransactionStatus.PENDING, "must not be marked completed"
        assert user.wallet_balance == Decimal("0.00")


def test_webhook_ignores_unpaid_status():
    txn_id = make_pending_transaction(invoice_id="1005", amount="10.00")

    webhook_server.process_invoice_paid({"invoice_id": "1005", "status": "active", "amount": "10.00"})

    with get_db_session() as session:
        txn = session.query(Transaction).filter_by(id=txn_id).first()
        assert txn.status == TransactionStatus.PENDING
