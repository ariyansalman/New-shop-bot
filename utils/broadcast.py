"""Sending one message to every user.

Three places do this - the two admin broadcasts and the twice-daily
availability job - and all three had the same two faults.

The first is that they treated every failure alike. Telegram answers a
burst with RetryAfter, meaning "wait N seconds and I will take it"; the
loops caught that as a plain exception and moved on, so a flood-limited
broadcast quietly dropped the rest of the burst. A user who has blocked
the bot raises Forbidden instead, which is permanent and worth counting
separately - an admin reading "412 sent, 88 failed" cannot tell a store
that lost 88 subscribers from one that hit a rate limit and lost nothing.

The second is length. The availability text lists up to fifteen products
for every category; at ten categories it is 7,281 characters and Telegram
refuses it, so a store that grew past nine categories broadcast to nobody
and the refusal was logged at debug level as if each user had blocked the
bot. Splitting is handled here so no caller has to remember.
"""

import asyncio
import logging
from dataclasses import dataclass

from telegram.error import Forbidden, RetryAfter, TelegramError

from .telegram_text import split_message

logger = logging.getLogger(__name__)

# 50ms between recipients is about 20 messages a second, under Telegram's 30.
PACE = 0.05
# A photo and its caption are two sends to the same chat; let the first land.
PHOTO_GAP = 0.03


@dataclass
class BroadcastResult:
    """What a broadcast actually achieved."""

    sent: int = 0
    blocked: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.blocked + self.failed

    def summary(self) -> str:
        lines = [f"✅ Sent successfully: {self.sent}"]
        if self.blocked:
            lines.append(f"🚫 Blocked the bot: {self.blocked}")
        lines.append(f"❌ Failed: {self.failed}")
        lines.append(f"👥 Total users: {self.total}")
        return "\n".join(lines)


async def _deliver(send, retries: int = 1) -> str:
    """One send, waiting out flood control rather than giving up on it.

    Returns 'sent', 'blocked' or 'failed'. Forbidden is permanent - the
    user blocked the bot or deleted the account - so it is never retried.
    """
    attempt = 0
    while True:
        try:
            await send()
            return "sent"
        except Forbidden:
            return "blocked"
        except RetryAfter as flood:
            if attempt >= retries:
                logger.warning("Broadcast still rate limited after waiting; giving up")
                return "failed"
            attempt += 1
            # retry_after is Telegram's own number; a second's margin covers
            # the round trip so the retry does not land early and re-trip it.
            await asyncio.sleep(float(flood.retry_after) + 1)
        except TelegramError as error:
            logger.debug("Broadcast send failed: %s", error)
            return "failed"
        except Exception as error:  # noqa: BLE001 - one bad chat must not end the run
            logger.debug("Broadcast send failed: %s", error)
            return "failed"


async def broadcast(bot, chat_ids, text: str, reply_markup=None,
                    photo=None, pace: float = PACE) -> BroadcastResult:
    """Send text (optionally after a photo) to every chat, and report.

    Long text continues into follow-up messages; the keyboard rides on the
    last one, where the reader ends up. A recipient who fails partway is
    counted once, by whatever stopped them.
    """
    parts = split_message(text)
    result = BroadcastResult()

    for chat_id in chat_ids:
        outcome = "sent"

        if photo is not None:
            outcome = await _deliver(
                lambda cid=chat_id: bot.send_photo(chat_id=cid, photo=photo))
            if outcome == "sent":
                await asyncio.sleep(PHOTO_GAP)

        if outcome == "sent":
            for index, part in enumerate(parts, start=1):
                markup = reply_markup if index == len(parts) else None
                outcome = await _deliver(
                    lambda cid=chat_id, p=part, m=markup: bot.send_message(
                        chat_id=cid, text=p, reply_markup=m))
                if outcome != "sent":
                    break

        setattr(result, outcome, getattr(result, outcome) + 1)
        await asyncio.sleep(pace)

    return result
