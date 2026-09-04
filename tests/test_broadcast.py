"""Broadcasting to every user.

Two faults this pins down. Telegram answers a burst with RetryAfter,
meaning "wait and I will take it"; the old loops counted that as a failed
send and dropped the rest of the burst. And the availability text lists
fifteen products per category, so at ten categories it is over 7,000
characters and Telegram refuses it - the store broadcast to nobody while
the log recorded each refusal at debug level as a blocked user.
"""

import asyncio
from decimal import Decimal

import pytest
from telegram.error import Forbidden, RetryAfter, TimedOut

from utils.broadcast import BroadcastResult, broadcast
from utils.helpers import build_availability_text
from utils.telegram_text import MAX_MESSAGE


class Recorder:
    """A bot that can be told to fail in specific ways for specific chats."""

    def __init__(self, failures=None):
        self.sent = []            # (chat_id, text, has_markup)
        self.photos = []          # chat_id
        self.failures = failures or {}   # chat_id -> list of exceptions
        self.slept = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        queued = self.failures.get(chat_id)
        if queued:
            raise queued.pop(0)
        self.sent.append((chat_id, text, reply_markup is not None))

    async def send_photo(self, chat_id, photo, **kwargs):
        queued = self.failures.get(chat_id)
        if queued:
            raise queued.pop(0)
        self.photos.append(chat_id)


@pytest.fixture
def no_waiting(monkeypatch):
    """Record sleeps instead of serving them, so tests stay fast."""
    slept = []
    real = asyncio.sleep

    async def _sleep(seconds):
        slept.append(seconds)
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


async def test_everyone_gets_the_message(no_waiting):
    bot = Recorder()
    result = await broadcast(bot, [1, 2, 3], "hello")

    assert [chat for chat, _text, _m in bot.sent] == [1, 2, 3]
    assert (result.sent, result.blocked, result.failed) == (3, 0, 0)


async def test_flood_control_is_waited_out_not_counted_as_a_failure(no_waiting):
    """The bug: RetryAfter dropped the recipient instead of retrying."""
    bot = Recorder(failures={2: [RetryAfter(7)]})

    result = await broadcast(bot, [1, 2, 3], "hello")

    assert (result.sent, result.failed) == (3, 0)
    assert [chat for chat, _t, _m in bot.sent] == [1, 2, 3]
    # Telegram's own number, plus a second's margin for the round trip.
    assert 8 in no_waiting


async def test_flood_control_twice_in_a_row_gives_up(no_waiting):
    bot = Recorder(failures={2: [RetryAfter(1), RetryAfter(1)]})

    result = await broadcast(bot, [1, 2], "hello")

    assert (result.sent, result.failed) == (1, 1)


async def test_a_user_who_blocked_the_bot_is_counted_apart(no_waiting):
    """"88 failed" reads as breakage; "88 blocked the bot" reads as churn."""
    bot = Recorder(failures={2: [Forbidden("bot was blocked by the user")]})

    result = await broadcast(bot, [1, 2, 3], "hello")

    assert (result.sent, result.blocked, result.failed) == (2, 1, 0)
    assert "🚫 Blocked the bot: 1" in result.summary()


async def test_a_block_is_never_retried(no_waiting):
    """Retrying a permanent refusal only slows the run down."""
    bot = Recorder(failures={1: [Forbidden("blocked"), Forbidden("blocked")]})

    await broadcast(bot, [1], "hello")

    assert len(bot.failures[1]) == 1     # only the first was consumed


async def test_one_bad_recipient_does_not_end_the_run(no_waiting):
    bot = Recorder(failures={2: [TimedOut()]})

    result = await broadcast(bot, [1, 2, 3], "hello")

    assert (result.sent, result.failed) == (2, 1)
    assert [chat for chat, _t, _m in bot.sent] == [1, 3]


async def test_a_long_message_is_split_rather_than_refused(no_waiting):
    """The availability broadcast, at the size a real store reaches."""
    class Product:
        def __init__(self, name):
            self.name, self.price, self.stock_count = name, Decimal("9.99"), 20

    text = build_availability_text({
        f"Category {c}": [Product(f"Product name {c}-{i}") for i in range(15)]
        for c in range(10)
    })
    assert len(text) > MAX_MESSAGE, "the fixture no longer reproduces the bug"

    bot = Recorder()
    result = await broadcast(bot, [1], text)

    parts = [body for _chat, body, _m in bot.sent]
    assert len(parts) > 1
    assert all(len(part) <= MAX_MESSAGE for part in parts)
    assert result.sent == 1


async def test_the_keyboard_rides_on_the_last_part(no_waiting):
    bot = Recorder()
    long_text = "\n".join(f"line {i}" for i in range(2000))

    await broadcast(bot, [1], long_text, reply_markup=object())

    carries = [has_markup for _chat, _text, has_markup in bot.sent]
    assert carries[-1] is True
    assert not any(carries[:-1])


async def test_a_short_message_stays_one_send(no_waiting):
    bot = Recorder()
    await broadcast(bot, [1], "just a line")
    assert len(bot.sent) == 1


async def test_an_image_broadcast_sends_the_photo_then_the_text(no_waiting):
    bot = Recorder()
    result = await broadcast(bot, [1, 2], "caption", photo="file-id")

    assert bot.photos == [1, 2]
    assert [chat for chat, _t, _m in bot.sent] == [1, 2]
    assert result.sent == 2


async def test_a_failed_photo_counts_the_recipient_once(no_waiting):
    """Not once for the photo and again for the text it never reached."""
    bot = Recorder(failures={1: [Forbidden("blocked")]})

    result = await broadcast(bot, [1], "caption", photo="file-id")

    assert (result.sent, result.blocked, result.failed) == (0, 1, 0)
    assert result.total == 1
    assert bot.sent == []          # the text was not attempted


async def test_the_run_is_paced(no_waiting):
    bot = Recorder()
    await broadcast(bot, [1, 2, 3], "hello")

    assert no_waiting.count(0.05) == 3      # one pause per recipient


def test_the_summary_hides_a_blocked_line_nobody_needs():
    assert "Blocked" not in BroadcastResult(sent=5).summary()
    assert BroadcastResult(sent=5).total == 5
