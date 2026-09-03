"""Keeping a message inside Telegram's length limit.

Telegram rejects a text message over 4096 characters outright. Every screen
that lists rows from the database can cross that: an order of 300 keys
builds a 6,700-character delivery message, and the send fails *after* the
wallet has been debited and the keys marked sold. The customer has paid,
the stock is gone, and the keys are unreachable - the order history view
is built the same way and is too long as well.

So a long message is split rather than sent and refused. Splitting happens
at line boundaries wherever possible, because these messages are lists:
breaking mid-key would hand someone half a licence key.
"""

# Telegram's own limit. Left a little headroom for the "(1/3)" marker that
# multi-part sends add.
MAX_MESSAGE = 4096
_HEADROOM = 32


def split_message(text: str, limit: int = MAX_MESSAGE) -> list:
    """Break text into chunks that Telegram will accept.

    Splits between lines. A single line longer than the limit - which no
    real licence key is, but a pasted blob might be - is hard-split rather
    than dropped.
    """
    limit = max(1, limit - _HEADROOM)
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


async def edit_or_split(query, text: str, reply_markup=None):
    """Replace the current message, continuing into follow-ups if needed.

    The keyboard goes on the last part, where the reader ends up.
    """
    parts = split_message(text)

    await query.edit_message_text(
        parts[0], reply_markup=reply_markup if len(parts) == 1 else None)

    for index, part in enumerate(parts[1:], start=2):
        last = index == len(parts)
        await query.message.reply_text(
            part, reply_markup=reply_markup if last else None)


async def send_or_split(bot, chat_id, text: str, reply_markup=None):
    """Send a message, continuing into follow-ups if it is too long."""
    parts = split_message(text)
    for index, part in enumerate(parts, start=1):
        last = index == len(parts)
        await bot.send_message(
            chat_id=chat_id, text=part,
            reply_markup=reply_markup if last else None)
