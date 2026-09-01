"""Store settings: support/channel username, welcome message, store logo.

Same asyncio.to_thread refactor as the other handler modules: every
"with get_db_session()" block's query/mutation work runs in a nested
_sync() closure off the event loop, returning only plain data, with the
final Telegram API call left on the event loop.
"""

import asyncio
import logging
import os
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, Settings
)
from utils import is_admin
from config.settings import settings as app_settings

logger = logging.getLogger(__name__)

# Conversation states
SETTING_VALUE = 0
WELCOME_MESSAGE, STORE_LOGO = range(2)


# ==================== SETTINGS CONFIGURATION ====================

async def config_support_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start support username configuration."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    context.user_data['setting_type'] = 'support_username'

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            return settings.support_username if settings and settings.support_username else "Not set"

    current_value = await asyncio.to_thread(_sync)

    await query.edit_message_text(
        f"📞 Current support username: @{current_value}\n\nEnter the new support Telegram username (without @):"
    )
    return SETTING_VALUE


async def config_channel_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start channel username configuration."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    context.user_data['setting_type'] = 'channel_username'

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            return settings.channel_username if settings and settings.channel_username else "Not set"

    current_value = await asyncio.to_thread(_sync)

    await query.edit_message_text(
        f"📢 Current channel username: @{current_value}\n\nEnter the new channel/group Telegram username (without @):"
    )
    return SETTING_VALUE


async def setting_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle setting value input."""
    setting_type = context.user_data['setting_type']
    value = update.message.text.strip().replace('@', '')

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()

            if not settings:
                settings = Settings()
                session.add(settings)

            if setting_type == 'support_username':
                settings.support_username = value
            elif setting_type == 'channel_username':
                settings.channel_username = value

            settings.updated_at = datetime.utcnow()
            session.commit()

    await asyncio.to_thread(_sync)

    if setting_type == 'support_username':
        await update.message.reply_text(f"✅ Support username set to: @{value}")
    elif setting_type == 'channel_username':
        await update.message.reply_text(f"✅ Channel username set to: @{value}")

    context.user_data.clear()
    return ConversationHandler.END


# ==================== WELCOME MESSAGE FLOW ====================

async def config_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start welcome message configuration flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            return settings.welcome_message if settings else "Welcome to our Digital Products Store!"

    current_msg = await asyncio.to_thread(_sync)

    from utils import create_cancel_keyboard
    await query.edit_message_text(
        f"💬 Welcome Message Configuration\n\n"
        f"Current message:\n{current_msg}\n\n"
        f"Please send the new welcome message:",
        reply_markup=create_cancel_keyboard()
    )
    context.user_data['setting_type'] = 'welcome_message'
    return WELCOME_MESSAGE


async def welcome_message_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new welcome message value."""
    new_message = update.message.text

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            if not settings:
                settings = Settings()
                session.add(settings)

            settings.welcome_message = new_message
            session.commit()

    await asyncio.to_thread(_sync)

    from utils import create_admin_settings_menu_keyboard
    await update.message.reply_text(
        f"✅ Welcome message updated successfully!\n\n"
        f"New message:\n{new_message}",
        reply_markup=create_admin_settings_menu_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


# ==================== STORE LOGO FLOW ====================

async def config_store_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start store logo configuration flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            return bool(settings and settings.store_logo_path and os.path.exists(settings.store_logo_path))

    has_logo = await asyncio.to_thread(_sync)

    from utils import create_cancel_keyboard
    status_text = "✅ Logo is set" if has_logo else "❌ No logo set"

    await query.edit_message_text(
        f"🖼 Store Logo Configuration\n\n"
        f"Current status: {status_text}\n\n"
        f"Please send a new image for the store logo:",
        reply_markup=create_cancel_keyboard()
    )
    context.user_data['setting_type'] = 'store_logo'
    return STORE_LOGO


async def store_logo_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new store logo image."""
    # Get the photo
    photo = update.message.photo[-1]  # Get highest resolution

    # Download the photo
    file = await context.bot.get_file(photo.file_id)

    # Use the configured logo directory instead of a hardcoded "uploads" path.
    logos_dir = app_settings.LOGOS_DIR
    os.makedirs(logos_dir, exist_ok=True)

    # Save the file
    file_path = os.path.join(logos_dir, f"store_logo_{photo.file_id}.jpg")
    await file.download_to_drive(file_path)

    def _sync():
        with get_db_session() as session:
            settings = session.query(Settings).first()
            if not settings:
                settings = Settings()
                session.add(settings)

            # Delete old logo if exists
            if settings.store_logo_path and os.path.exists(settings.store_logo_path):
                try:
                    os.remove(settings.store_logo_path)
                except OSError:
                    pass

            settings.store_logo_path = file_path
            session.commit()

    await asyncio.to_thread(_sync)

    from utils import create_admin_settings_menu_keyboard
    await update.message.reply_text(
        "✅ Store logo updated successfully!",
        reply_markup=create_admin_settings_menu_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel settings configuration flow."""
    from utils import create_admin_settings_menu_keyboard

    # Handle both callback query and message
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Configuration cancelled.",
            reply_markup=create_admin_settings_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Configuration cancelled.",
            reply_markup=create_admin_settings_menu_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END
