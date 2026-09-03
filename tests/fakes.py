"""Minimal fake python-telegram-bot objects.

Just enough surface for the handlers under test: `query.data`,
`query.answer()`, `query.edit_message_text()`, `update.effective_user.id`,
and `context.bot.send_message()`. Not a general-purpose PTB test double -
add attributes as new tests need them.
"""


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeQuery:
    def __init__(self, data: str, user_id: int, message_has_photo: bool = False):
        self.data = data
        self.answers = []  # list of (text, kwargs)
        self.edits = []  # list of (text, reply_markup)

        class _Msg:
            photo = message_has_photo

            def __init__(self, parent):
                self._parent = parent

            async def reply_text(self, text, reply_markup=None, **kwargs):
                # A long answer continues into follow-up messages; record
                # them on the query so a test sees the whole thing.
                self._parent.edits.append((text, reply_markup))

            async def delete(self):
                pass

        self.message = _Msg(self)

    async def answer(self, text: str = None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    @property
    def last_answer(self):
        return self.answers[-1] if self.answers else None

    @property
    def last_edit_text(self):
        return self.edits[-1][0] if self.edits else None


class FakeBot:
    def __init__(self):
        self.sent = []  # list of (chat_id, text)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}


class FakeUpdate:
    def __init__(self, query: FakeQuery, user_id: int):
        self.callback_query = query
        self.effective_user = FakeUser(user_id)


class FakeMessage:
    """For MessageHandler-based handlers (restock paste/file, not callback buttons)."""

    def __init__(self, text: str = None, document=None):
        self.text = text
        self.document = document
        self.replies = []  # list of (text, reply_markup)

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))

    @property
    def last_reply_text(self):
        return self.replies[-1][0] if self.replies else None


class FakeMessageUpdate:
    """update object for MessageHandler-based handlers."""

    def __init__(self, message: FakeMessage, user_id: int):
        self.message = message
        self.effective_user = FakeUser(user_id)


def set_switch(key, value, monkeypatch):
    """Set one payment method's admin switch for the duration of a test.

    The switches are a process-level cache in services.payment_methods, so
    setting one directly would leak into every test that follows.
    monkeypatch.setitem restores it afterwards.

    value: True/False for an admin decision, None for "not decided" (which
    makes the method follow its environment variable).
    """
    from services import payment_methods
    monkeypatch.setitem(payment_methods._switches, key, value)
