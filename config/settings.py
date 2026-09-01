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
    DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', 5))
    DB_MAX_OVERFLOW = int(os.getenv('DB_MAX_OVERFLOW', 5))
    # Recycle below any server/proxy idle timeout so we never hand out a
    # connection the server has already closed.
    DB_POOL_RECYCLE = int(os.getenv('DB_POOL_RECYCLE', 900))
    DB_ECHO = _as_bool(os.getenv('DB_ECHO'), False)

    # Crypto Payment Settings
    CRYPTO_BOT_API_KEY = os.getenv('CRYPTO_BOT_API_KEY', '')

    # Telegram Payments (Card) Settings
    # Provider token from @BotFather -> your bot -> Payments -> connect a provider.
    TELEGRAM_PROVIDER_TOKEN = os.getenv('TELEGRAM_PROVIDER_TOKEN', '')
    # Currency the card invoice is charged in. The numeric amount equals the USD
    # top-up value, so this must be a USD-denominated provider for amounts to match.
    PAYMENT_CURRENCY = os.getenv('PAYMENT_CURRENCY', 'USD')

    # Application Settings
    PAYMENT_EXPIRY_HOURS = float(os.getenv('PAYMENT_EXPIRY_HOURS', 0.5))  # 30 minutes
    PAYMENT_CHECK_INTERVAL = int(os.getenv('PAYMENT_CHECK_INTERVAL', 30))  # Seconds between checks
    MIN_TOPUP_AMOUNT = float(os.getenv('MIN_TOPUP_AMOUNT', 1.0))
    MAX_TOPUP_AMOUNT = float(os.getenv('MAX_TOPUP_AMOUNT', 100000.0))

    # HTTP server (CryptoBot webhook + Railway healthcheck).
    # Railway injects PORT; binding it is what gives the service a public URL.
    PORT = int(os.getenv('PORT', 8080))
    WEBHOOK_ENABLED = _as_bool(os.getenv('WEBHOOK_ENABLED'), True)

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

    # Non-fatal, but the corresponding payment method will not work.
    if not settings.CRYPTO_BOT_API_KEY:
        logger.warning("CRYPTO_BOT_API_KEY is not set - crypto top-ups are disabled")
    if not settings.TELEGRAM_PROVIDER_TOKEN:
        logger.warning("TELEGRAM_PROVIDER_TOKEN is not set - card top-ups are disabled")
    if not settings.BOT_USERNAME:
        logger.warning("BOT_USERNAME is not set - CryptoBot 'return to bot' button disabled")

    logger.info("Configuration validated successfully")
