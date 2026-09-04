"""Configuration settings loader from environment variables."""

import logging
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables from a local .env file.
# On Railway the variables come from the service environment instead, and there
# is no .env file - load_dotenv() simply does nothing in that case.
load_dotenv()


def _normalize_database_url(raw: str) -> str:
    """Make a Supabase/Railway connection string usable by SQLAlchemy.

    Supabase (and Heroku-style providers) hand out URLs beginning with
    ``postgres://``, which SQLAlchemy 2.x no longer recognises. We also pin the
    driver explicitly and force TLS, which Supabase requires.
    """
    if not raw:
        return 'sqlite:///bot_database.db'

    url = raw.strip()

    # postgres:// -> postgresql+psycopg2://
    if url.startswith('postgres://'):
        url = 'postgresql+psycopg2://' + url[len('postgres://'):]
    elif url.startswith('postgresql://'):
        url = 'postgresql+psycopg2://' + url[len('postgresql://'):]

    if not url.startswith('postgresql+psycopg2://'):
        return url  # sqlite, mysql, or an already-qualified URL - leave as-is

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Supabase rejects unencrypted connections; add sslmode if absent.
    query.setdefault('sslmode', 'require')
    # Identify this app in Supabase's connection logs.
    query.setdefault('application_name', 'telegram-store-bot')

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _as_number(name: str, default, cast):
    """Read a numeric env var, falling back to the default on garbage.

    A typo'd value (DB_POOL_SIZE=five) used to raise a bare ValueError while
    this module was still being imported, so the bot died before any of the
    readable configuration errors in validate_settings() could run - and the
    traceback pointed at settings.py rather than at the variable. Same
    treatment ADMIN_TELEGRAM_ID already got.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning("%s=%r is not a valid number - falling back to %r", name, raw, default)
        return default


class Settings:
    """Stores all configuration settings for the bot."""

    # Telegram Bot Settings
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    # A non-numeric ADMIN_TELEGRAM_ID used to crash at import time with a bare
    # ValueError; validate_settings() reports it properly instead.
    try:
        ADMIN_TELEGRAM_ID = int(os.getenv('ADMIN_TELEGRAM_ID') or 0)
    except ValueError:
        ADMIN_TELEGRAM_ID = 0
    ADMIN_TELEGRAM_USERNAME = os.getenv('ADMIN_TELEGRAM_USERNAME', '')

    # Extra admins beyond ADMIN_TELEGRAM_ID, comma-separated
    # (e.g. "111111,222222"). ADMIN_TELEGRAM_ID stays as the single "primary"
    # admin - the one broadcast completion / new-order notifications go to -
    # this just widens who is_admin() accepts. Invalid entries are dropped
    # with a warning rather than crashing the whole bot at import time.
    ADMIN_IDS = set()
    if ADMIN_TELEGRAM_ID:
        ADMIN_IDS.add(ADMIN_TELEGRAM_ID)
    for _raw_id in os.getenv('ADMIN_TELEGRAM_IDS', '').split(','):
        _raw_id = _raw_id.strip()
        if not _raw_id:
            continue
        try:
            ADMIN_IDS.add(int(_raw_id))
        except ValueError:
            logger.warning("Ignoring non-numeric entry in ADMIN_TELEGRAM_IDS: %r", _raw_id)
    del _raw_id
    # Used to build the CryptoBot "return to bot" button (without the leading @).
    BOT_USERNAME = os.getenv('BOT_USERNAME', '').lstrip('@')

    # Database Settings
    # On Railway set DATABASE_URL to the Supabase connection string.
    DATABASE_URL = _normalize_database_url(
        os.getenv('DATABASE_URL', 'sqlite:///bot_database.db')
    )

    # Connection pool sizing. Supabase allows a limited number of concurrent
    # connections, so keep the pool small.
    DB_POOL_SIZE = _as_number('DB_POOL_SIZE', 5, int)
    DB_MAX_OVERFLOW = _as_number('DB_MAX_OVERFLOW', 5, int)
    # Recycle below any server/proxy idle timeout so we never hand out a
    # connection the server has already closed.
    DB_POOL_RECYCLE = _as_number('DB_POOL_RECYCLE', 900, int)
    DB_ECHO = _as_bool(os.getenv('DB_ECHO'), False)

    # Crypto Payment Settings
    CRYPTO_BOT_API_KEY = os.getenv('CRYPTO_BOT_API_KEY', '')

    # Telegram Payments (Card) Settings
    # Provider token from @BotFather -> your bot -> Payments -> connect a provider.
    TELEGRAM_PROVIDER_TOKEN = os.getenv('TELEGRAM_PROVIDER_TOKEN', '')
    # Currency the card invoice is charged in. The numeric amount equals the USD
    # top-up value, so this must be a USD-denominated provider for amounts to match.
    PAYMENT_CURRENCY = os.getenv('PAYMENT_CURRENCY', 'USD')

    # Binance Pay Settings
    #
    # These are an ordinary Binance account API key/secret (the same kind
    # every signed SAPI endpoint takes), NOT Binance Pay *Merchant*
    # credentials - see services/binance_pay.py for why that distinction
    # matters. Give the key read permission only: verification never needs
    # to move funds, and a key that cannot withdraw cannot drain the
    # account if this server is ever compromised. Whitelist the server IP
    # in Binance's API management too.
    #
    # They live in the environment, not in the database and not in the
    # Telegram admin panel: a secret typed into Telegram ends up in
    # Telegram's servers, the chat history and the admin's phone backup.
    # The admin panel shows only a masked status and a test button.
    BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
    BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
    # The Binance Pay ID users send to. Not a secret - it is printed in the
    # checkout message on purpose.
    BINANCE_PAY_ID = os.getenv('BINANCE_PAY_ID', '')
    BINANCE_PAY_CURRENCY = os.getenv('BINANCE_PAY_CURRENCY', 'USDT').upper()
    BINANCE_PAY_ENABLED = _as_bool(os.getenv('BINANCE_PAY_ENABLED'), False)
    # Uses services/binance_pay_mock.py instead of the real API. A mock
    # success can only happen while this is on.
    BINANCE_TEST_MODE = _as_bool(os.getenv('BINANCE_TEST_MODE'), False)
    # Retry budget for a payment stuck on temporary errors. After the last
    # attempt the transaction goes to MANUAL_REVIEW - never to COMPLETED.
    BINANCE_MAX_VERIFY_ATTEMPTS = _as_number('BINANCE_MAX_VERIFY_ATTEMPTS', 5, int)
    # Seconds between background retries. The Pay history endpoint is
    # Weight(UID) 3000, so this is deliberately far slower than the
    # 30-second CryptoBot poll.
    BINANCE_VERIFY_RETRY_INTERVAL = _as_number('BINANCE_VERIFY_RETRY_INTERVAL', 180, int)

    # Application Settings
    PAYMENT_EXPIRY_HOURS = _as_number('PAYMENT_EXPIRY_HOURS', 0.5, float)  # 30 minutes
    PAYMENT_CHECK_INTERVAL = _as_number('PAYMENT_CHECK_INTERVAL', 30, int)  # Seconds between checks
    MIN_TOPUP_AMOUNT = _as_number('MIN_TOPUP_AMOUNT', 1.0, float)
    MAX_TOPUP_AMOUNT = _as_number('MAX_TOPUP_AMOUNT', 100000.0, float)

    # HTTP server (CryptoBot webhook + Railway healthcheck).
    # Railway injects PORT; binding it is what gives the service a public URL.
    PORT = _as_number('PORT', 8080, int)
    WEBHOOK_ENABLED = _as_bool(os.getenv('WEBHOOK_ENABLED'), True)

    # Error monitoring (optional). See config/monitoring.py - leaving this
    # unset disables Sentry entirely, no other behavior changes.
    SENTRY_DSN = os.getenv('SENTRY_DSN', '')

    # Asset Storage
    # IMPORTANT: Railway containers have an ephemeral filesystem - anything
    # written here is lost on redeploy. Mount a Railway Volume and set
    # ASSETS_DIR=/data/assets to keep uploaded logos and product images.
    ASSETS_DIR = os.getenv('ASSETS_DIR', 'assets')
    LOGOS_DIR = os.path.join(ASSETS_DIR, 'logos')
    PRODUCTS_DIR = os.path.join(ASSETS_DIR, 'products')


# Create settings instance
settings = Settings()


def validate_settings():
    """Validates that all required settings are configured."""
    if not settings.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required (set it in .env or in Railway variables)")

    if not settings.ADMIN_TELEGRAM_ID:
        raise ValueError("ADMIN_TELEGRAM_ID is required and must be a number")

    if len(settings.ADMIN_IDS) > 1:
        logger.info("%d admins configured (ADMIN_TELEGRAM_ID + ADMIN_TELEGRAM_IDS)",
                    len(settings.ADMIN_IDS))

    if not settings.DATABASE_URL:
        raise ValueError("DATABASE_URL is required")

    if settings.DATABASE_URL.startswith('sqlite'):
        logger.warning("Using SQLite. On Railway the filesystem is ephemeral, so the "
                        "database is wiped on every redeploy - set DATABASE_URL to your "
                        "Supabase connection string.")

    # Uploaded product images and the store logo are written to ASSETS_DIR.
    # On Railway (and any container platform) that path is wiped on every
    # redeploy unless it is a mounted volume, so the store silently loses
    # every image an admin uploaded. A relative path is always container
    # local; an absolute one is at least plausibly a mount point.
    if not os.path.isabs(settings.ASSETS_DIR):
        logger.warning(
            "ASSETS_DIR=%r is a relative path inside the container - uploaded "
            "product images and the store logo will be LOST on every redeploy. "
            "Mount a volume and set ASSETS_DIR to its path (e.g. /data/assets); "
            "see DEPLOY.md.",
            settings.ASSETS_DIR,
        )

    # Non-fatal, but the corresponding payment method will not work.
    if not settings.CRYPTO_BOT_API_KEY:
        logger.warning("CRYPTO_BOT_API_KEY is not set - crypto top-ups are disabled")
    if not settings.TELEGRAM_PROVIDER_TOKEN:
        logger.warning("TELEGRAM_PROVIDER_TOKEN is not set - card top-ups are disabled")

    # Binance Pay: warn about a half-configured setup rather than letting a
    # user reach a checkout that can never verify. Never log the values.
    if settings.BINANCE_PAY_ENABLED:
        missing = []
        if not settings.BINANCE_PAY_ID:
            missing.append("BINANCE_PAY_ID")
        if not settings.BINANCE_TEST_MODE:
            if not settings.BINANCE_API_KEY:
                missing.append("BINANCE_API_KEY")
            if not settings.BINANCE_API_SECRET:
                missing.append("BINANCE_API_SECRET")
        if missing:
            logger.warning(
                "BINANCE_PAY_ENABLED is on but %s not set - Binance top-ups "
                "will be hidden until configured", ", ".join(missing)
            )
        elif settings.BINANCE_TEST_MODE:
            logger.warning(
                "BINANCE_TEST_MODE is ON - Binance payments are verified against "
                "a mock provider, not the real Binance API. Never leave this on "
                "in production."
            )
    if not settings.BOT_USERNAME:
        logger.warning("BOT_USERNAME is not set - CryptoBot 'return to bot' button disabled")

    logger.info("Configuration validated successfully")
