"""Telling a customer something, when they may not be reachable.

Five places message a customer as the tail of an admin action. All five
wrapped the send in `except Exception: pass`, which is the right shape -
an admin confirming a payment must not fail because that customer blocked
the bot - but it was silent. The admin saw the action succeed with no way
to know the customer was never told, and the log said nothing either.
"""

import logging

import pytest
from telegram.error import Forbidden, TimedOut

from utils.notify import UNREACHABLE, notify_user


class Bot:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text))


async def test_a_reachable_user_gets_the_message():
    bot = Bot()
    assert await notify_user(bot, 555, "hello") is True
    assert bot.sent == [(555, "hello")]


async def test_a_user_who_blocked_the_bot_reports_false_not_raises():
    """The admin action already committed; it must still finish."""
    bot = Bot(Forbidden("bot was blocked by the user"))
    assert await notify_user(bot, 555, "hello") is False


async def test_a_transient_error_reports_false_not_raises():
    bot = Bot(TimedOut())
    assert await notify_user(bot, 555, "hello") is False


async def test_a_non_telegram_error_is_still_contained():
    bot = Bot(RuntimeError("something else entirely"))
    assert await notify_user(bot, 555, "hello") is False


async def test_a_missing_recipient_is_not_an_error():
    bot = Bot()
    assert await notify_user(bot, None, "hello") is False
    assert bot.sent == []


async def test_a_block_is_logged_so_a_support_ticket_has_a_trail(caplog):
    """The bug was silence: nothing recorded that the send never landed."""
    with caplog.at_level(logging.INFO, logger="utils.notify"):
        await notify_user(Bot(Forbidden("blocked")), 555, "hello")

    assert any("555" in record.getMessage() for record in caplog.records)


async def test_a_transient_failure_is_logged_louder_than_a_block(caplog):
    with caplog.at_level(logging.DEBUG, logger="utils.notify"):
        await notify_user(Bot(Forbidden("blocked")), 1, "x")
        await notify_user(Bot(TimedOut()), 2, "x")

    levels = {record.getMessage()[:20]: record.levelno for record in caplog.records}
    assert len(caplog.records) == 2
    assert sorted(levels.values()) == [logging.INFO, logging.WARNING]


def test_the_admin_is_told_the_action_itself_went_through():
    """An operator reading this must not think the payment was rolled back."""
    assert "could not be notified" in UNREACHABLE
    assert "went through" in UNREACHABLE


@pytest.mark.parametrize("module,name", [
    ("handlers.admin_handlers", "admin_confirm_payment_callback"),
    ("handlers.admin_handlers", "admin_cancel_payment_callback"),
    ("handlers.admin_handlers", "admin_cancel_order_callback"),
    ("handlers.payment_handlers", "check_pending_payments"),
    ("handlers.binance_admin", "binance_admin_retry"),
])
def test_no_customer_notification_is_swallowed_silently(module, name):
    """Pins the five sites that used to be `except Exception: pass`."""
    import ast
    import importlib
    import inspect

    target = getattr(importlib.import_module(module), name, None)
    if target is None:
        pytest.skip(f"{module}.{name} has been renamed")

    tree = ast.parse(inspect.getsource(target))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and getattr(node.func, "attr", None) == "send_message"
             and any(kw.arg == "chat_id" for kw in node.keywords)]
    assert not calls, (
        f"{module}.{name} messages a customer directly; use notify_user so "
        "an unreachable customer is logged and surfaced to the admin")
