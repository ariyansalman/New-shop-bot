"""Shared pieces of the admin screens.

Both of these were written out separately in each admin module, and both
are security controls: one decides who gets in, the other decides what a
secret looks like on screen. Three copies of an authorization gate is
three places to fix when it is wrong, and the copy nobody remembers is the
one that stays wrong.
"""

from telegram import Update

from .helpers import is_admin


async def deny_if_not_admin(update: Update) -> bool:
    """Refuse a non-admin with an alert. True when the caller should stop.

    Answers the callback itself, before any other answer. Telegram accepts
    one answer per callback and silently discards the rest, so a handler
    that answers first would make this alert invisible and the button
    would simply look dead.
    """
    if is_admin(update.effective_user.id):
        return False
    await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    return True


def mask_secret(value: str) -> str:
    """Report that a secret is set without disclosing it.

    The last four characters are enough for an admin to tell which key is
    loaded and not enough to use it. Anything shorter than that is shown
    only as set, because four characters of a four-character value is the
    whole value.
    """
    if not value:
        return "❌ not set"
    if len(value) <= 4:
        return "✅ set"
    return f"✅ set (…{value[-4:]})"
