"""Optional Sentry error monitoring.

Off by default - init_sentry() is a no-op unless SENTRY_DSN is set. When it
is, sentry-sdk's LoggingIntegration hooks into the stdlib logging module,
so every ERROR-level call the app already makes (logger.exception(...) in
handlers, bot.py's global error_handler logging context.error, etc.) is
reported to Sentry automatically. No per-call sentry_sdk.capture_exception()
changes needed anywhere else in the codebase.
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)


def init_sentry():
    """Call once, early, before building the bot or the webhook server."""
    if not settings.SENTRY_DSN:
        return

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[
            # Every logger.error()/logger.exception() call becomes a
            # Sentry event; INFO and below stay local-only breadcrumbs.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            FlaskIntegration(),
        ],
        # Error tracking only, no performance/trace sampling - this is a
        # low-traffic bot, not a service that needs latency breakdowns.
        traces_sample_rate=0.0,
    )
    logger.info("Sentry error monitoring enabled")
