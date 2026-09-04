"""One description of each top-up method, and one switch mechanism for all.

Before this, Binance had a database-backed on/off switch and an admin
screen, while CryptoBot and Card had neither - their only control was an
environment variable and a redeploy. The rules were also written three
times over: "is it configured", "should it be offered", "which variable is
missing" each existed once per method, in a different file, in a different
shape.

The switch works the same way for all three, and the same way it already
did for Binance:

    column is NULL  ->  no admin has decided, so the environment variable
                        supplies the answer
    column is set   ->  an admin decided, and their choice outlives the
                        environment variable

A switch can only ever narrow what the server can actually do. Turning a
method on still requires its credentials, which no Telegram button can
supply - so `available()` is `configured() and is_on()`, never one or the
other.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from config.settings import settings
from database import get_db_session, Settings as StoreSettings
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentMethodSpec:
    key: str                     # short id used in callback data
    label: str                   # what an admin and a customer see
    callback: str                # the user-facing "pay with this" callback
    column: str                  # Settings column holding the admin switch
    env_var: str                 # the environment variable it defaults to
    _configured: Callable        # whether credentials are present
    _missing: Callable           # which environment values are missing
    notes: tuple = field(default=())   # shown on the admin screen

    @property
    def name(self) -> str:
        """The label without its emoji, for use inside a sentence or a
        button that carries an emoji of its own."""
        return self.label.split(" ", 1)[-1]

    @property
    def env_default(self) -> bool:
        return bool(getattr(settings, self.env_var, False))

    def configured(self) -> bool:
        return bool(self._configured())

    def missing(self) -> list:
        return list(self._missing())


def _crypto_missing():
    return [] if settings.CRYPTO_BOT_API_KEY else ["CRYPTO_BOT_API_KEY"]


def _card_missing():
    return [] if settings.TELEGRAM_PROVIDER_TOKEN else ["TELEGRAM_PROVIDER_TOKEN"]


def _binance_missing():
    missing = []
    if not settings.BINANCE_PAY_ID:
        missing.append("BINANCE_PAY_ID")
    # Test mode replaces the network call, so it needs no real key.
    if not settings.BINANCE_TEST_MODE:
        if not settings.BINANCE_API_KEY:
            missing.append("BINANCE_API_KEY")
        if not settings.BINANCE_API_SECRET:
            missing.append("BINANCE_API_SECRET")
    return missing


SPECS = (
    PaymentMethodSpec(
        key="crypto",
        label="🪙 CryptoBot",
        callback="pay_crypto",
        column="crypto_pay_enabled",
        # CryptoBot has no dedicated on/off variable: holding the API key
        # has always been what enabled it, so that is its default.
        env_var="CRYPTO_BOT_API_KEY",
        _configured=lambda: bool(settings.CRYPTO_BOT_API_KEY),
        _missing=_crypto_missing,
        notes=("Any coin CryptoBot supports.",
               "Confirmed by webhook, plus a 30s poll."),
    ),
    PaymentMethodSpec(
        key="card",
        label="💳 Card",
        callback="pay_card",
        column="card_pay_enabled",
        env_var="TELEGRAM_PROVIDER_TOKEN",
        _configured=lambda: bool(settings.TELEGRAM_PROVIDER_TOKEN),
        _missing=_card_missing,
        notes=("Telegram Payments, via your provider.",
               "Charged in PAYMENT_CURRENCY."),
    ),
    PaymentMethodSpec(
        key="binance",
        label="🟡 Binance Pay",
        callback="pay_binance",
        column="binance_pay_enabled",
        env_var="BINANCE_PAY_ENABLED",
        _configured=lambda: bool(
            settings.BINANCE_PAY_ID
            and (settings.BINANCE_TEST_MODE
                 or (settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET))),
        _missing=_binance_missing,
        notes=("Buyer submits the Binance Transaction ID.",
               "Verified against Pay history, then credited."),
    ),
)

BY_KEY = {s.key: s for s in SPECS}

# The admin switches, cached in this process. None means "not decided", so
# the environment variable answers. Loaded once at startup by refresh(),
# and written only by set_switch_sync() below.
_switches = {s.key: None for s in SPECS}


def spec(key: str):
    return BY_KEY.get(key)


def is_on(key: str) -> bool:
    """The live on/off: the admin's choice, else the environment."""
    decided = _switches.get(key)
    if decided is None:
        s = BY_KEY.get(key)
        return s.env_default if s else False
    return decided


def switch_state(key: str):
    """The stored switch itself: True, False, or None for 'not decided'."""
    return _switches.get(key)


def configured(key: str) -> bool:
    s = BY_KEY.get(key)
    return bool(s and s.configured())


def available(key: str) -> bool:
    """Whether to offer this method to customers at all."""
    return configured(key) and is_on(key)


def available_specs() -> list:
    return [s for s in SPECS if available(s.key)]


def read_switches_sync() -> dict:
    """Load every switch from the database. Call from a thread."""
    with get_db_session() as session:
        row = session.query(StoreSettings).first()
        for s in SPECS:
            _switches[s.key] = None if row is None else getattr(row, s.column, None)
    return dict(_switches)


def set_switch_sync(key: str, enabled: bool, admin_telegram_id: int) -> bool:
    """Persist one switch and update the cache. Call from a thread."""
    s = BY_KEY[key]
    with get_db_session() as session:
        row = session.query(StoreSettings).first()
        if row is None:
            row = StoreSettings()
            session.add(row)
        setattr(row, s.column, enabled)
        log_admin_action(
            session, admin_telegram_id,
            f"payment_{key}_{'enable' if enabled else 'disable'}",
            target_type="settings",
        )
        session.commit()
    _switches[key] = enabled
    return enabled


def refresh() -> dict:
    """Load the switches at startup, before any update is served."""
    try:
        return read_switches_sync()
    except Exception:
        # A settings table that predates the columns, or an unreachable
        # database at boot. Falling back to "not decided" means the
        # environment configuration applies, which is the behaviour these
        # switches started from: they exist to narrow a working setup, not
        # to be a second thing that can silently break one.
        logger.warning("Could not load the payment switches - using the "
                       "environment configuration only", exc_info=True)
        return dict(_switches)
