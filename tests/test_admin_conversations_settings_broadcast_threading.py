"""handlers.admin_conversations.store_settings / broadcast - regression
tests for the asyncio.to_thread refactor.
"""

from database import get_db_session, User, Settings
from handlers.admin_conversations.store_settings import (
    config_support_username, setting_value, welcome_message_value,
)
from handlers.admin_conversations.broadcast import broadcast_text_message
from telegram.ext import ConversationHandler
from fakes import FakeUpdate, FakeQuery, FakeContext, FakeMessage, FakeMessageUpdate

ADMIN_ID = 1000000001  # matches conftest.py's ADMIN_TELEGRAM_ID


async def test_config_support_username_shows_current_value():
    with get_db_session() as session:
        session.add(Settings(support_username="oldsupport"))

    query = FakeQuery(data="admin_support_username", user_id=ADMIN_ID)
    update = FakeUpdate(query, ADMIN_ID)
    context = FakeContext()

    result = await config_support_username(update, context)

    assert "@oldsupport" in query.last_edit_text
    assert context.user_data['setting_type'] == 'support_username'
    assert result == 0  # SETTING_VALUE


async def test_setting_value_creates_settings_row_if_missing():
    message = FakeMessage(text="newsupport")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()
    context.user_data['setting_type'] = 'support_username'

    result = await setting_value(update, context)

    with get_db_session() as session:
        settings = session.query(Settings).first()
        assert settings is not None
        assert settings.support_username == "newsupport"

    assert "@newsupport" in message.last_reply_text
    assert context.user_data == {}
    assert result == ConversationHandler.END


async def test_welcome_message_value_updates_existing_settings():
    with get_db_session() as session:
        session.add(Settings(welcome_message="Old welcome"))

    message = FakeMessage(text="New welcome message!")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()

    await welcome_message_value(update, context)

    with get_db_session() as session:
        settings = session.query(Settings).first()
        assert settings.welcome_message == "New welcome message!"

    assert "updated successfully" in message.last_reply_text


async def test_broadcast_text_message_skips_banned_users():
    with get_db_session() as session:
        session.add(User(telegram_id=1, is_banned=False))
        session.add(User(telegram_id=2, is_banned=True))

    message = FakeMessage(text="Hello everyone!")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()

    await broadcast_text_message(update, context)

    sent_chat_ids = [chat_id for chat_id, _ in context.bot.sent]
    assert sent_chat_ids == [1]  # banned user (2) excluded
    assert "Sent successfully: 1" in message.last_reply_text


async def test_broadcast_text_message_no_users_shows_error():
    message = FakeMessage(text="Hello!")
    update = FakeMessageUpdate(message, ADMIN_ID)
    context = FakeContext()

    result = await broadcast_text_message(update, context)

    assert "No users found" in message.last_reply_text
    assert result == ConversationHandler.END
