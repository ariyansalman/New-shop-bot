"""Telling a customer something, when they may not be reachable.

Five places message a customer as the tail of an admin action: payment
confirmed, payment cancelled, order cancelled, top-up credited, Binance
payment settled. All five wrapped the send in `except Exception: pass`,
and swallowing is the right shape - an admin confirming a payment must
not fail because that customer blocked the bot.

Swallowing *silently* is not. The admin saw the action succeed and had no
way to know the customer was never told, and the log said nothing either,
so a support ticket ("I was never notified") had no trail behind it.

So the send still cannot raise, but it now says whether it landed, and
the admin screens tell the operator when it did not.
"""

import logging

from telegram.error import Forbidden, TelegramError

logger = logging.getLogger(__name__)


async def notify_user(bot, telegram_id, text: str) -> bool:
    """Send text to a user. True when it landed; never raises.

    A missing telegram_id counts as not landing rather than as an error -
    some rows genuinely have none.
    """
    if not telegram_id:
        return False
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        return True
    except Forbidden:
        # Permanent and ordinary: they blocked the bot or deleted the
        # account. Worth a line, not a warning.
        logger.info("User %s could not be notified: they blocked the bot",
                    telegram_id)
        return False
    except TelegramError as error:
        logger.warning("Could not notify user %s: %s", telegram_id, error)
        return False
    except Exception as error:  # noqa: BLE001 - an admin action must still finish
        logger.warning("Could not notify user %s: %s", telegram_id, error)
        return False


UNREACHABLE = ("\n\n⚠️ The customer could not be notified - they have "
               "blocked the bot or their account is gone. The action itself "
               "went through.")
