"""Railway entrypoint: runs the Telegram bot and the CryptoBot webhook together.

Railway gives a service a public URL only if it binds $PORT, and its healthcheck
needs an HTTP endpoint. Running both in one process therefore has two benefits:

  * one Railway service instead of two (and one Supabase connection pool), and
  * the webhook can push a "payment confirmed" message to the user immediately,
    because it shares the bot instance.

The bot uses long polling, so it must never run in more than one replica --
keep numReplicas at 1 (see railway.json).

Local use:  python app.py
"""

import asyncio
import logging
import os
import sys
import threading

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("waitress").setLevel(logging.WARNING)

logger = logging.getLogger("app")

from config import settings, validate_settings, init_sentry  # noqa: E402
from database.init_data import initialize_database      # noqa: E402
from telegram import Update                              # noqa: E402
import bot as bot_module                                # noqa: E402
import webhook_server                                   # noqa: E402
from services import payment_methods, store_content                    # noqa: E402


def _make_threadsafe_notifier(application, loop):
    """Build a notify(telegram_id, text) callable usable from the Flask thread.

    The Flask server runs on its own threads, but PTB's bot object must be used
    from the bot's event loop -- so hop across with run_coroutine_threadsafe.
    """
    def notify(telegram_id: int, text: str):
        async def _send():
            try:
                await application.bot.send_message(chat_id=telegram_id, text=text)
            except Exception as e:
                # User blocked the bot, deleted the chat, etc.
                logger.warning("Could not message %s: %s", telegram_id, e)

        try:
            asyncio.run_coroutine_threadsafe(_send(), loop)
        except Exception:
            logger.exception("Failed to schedule notification for %s", telegram_id)

    return notify


def main():
    # 1. Configuration
    try:
        validate_settings()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    init_sentry()

    # 2. Database (Supabase in production). Fail loudly: a bot running against
    #    an unreachable database would break on the very first message.
    try:
        initialize_database()
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        sys.exit(1)

    # 2b. Load the payment method switches once, before any update is
    #     served. They are read by keyboard builders on the event loop, so
    #     they must never hit the database themselves.
    payment_methods.refresh()
    store_content.refresh()

    # 3. Make sure the asset directories exist (a Railway Volume mounts empty).
    for directory in (settings.ASSETS_DIR, settings.LOGOS_DIR, settings.PRODUCTS_DIR):
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception as e:
            logger.warning("Could not create %s: %s", directory, e)

    # 4. Start the HTTP server once the bot's event loop is running, so the
    #    webhook always has a live loop to schedule notifications onto.
    async def _post_init(app_):
        if not settings.WEBHOOK_ENABLED:
            logger.info("WEBHOOK_ENABLED=false - HTTP server not started")
            return

        loop = asyncio.get_running_loop()
        webhook_server.set_notifier(_make_threadsafe_notifier(app_, loop))

        thread = threading.Thread(
            target=webhook_server.run_server,
            kwargs={"port": settings.PORT},
            name="webhook-server",
            daemon=True,
        )
        thread.start()
        logger.info("HTTP server thread started on port %s", settings.PORT)

    # 5. Bot
    application = bot_module.build_application(post_init=_post_init)

    logger.info("Starting bot (long polling)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        # Ignore updates queued while the service was redeploying.
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
