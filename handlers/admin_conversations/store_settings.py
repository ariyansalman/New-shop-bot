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
WELCOME_MESSAGE, STORE_LOGO, TERMS_TEXT, REFERRAL_BONUS = range(4)


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


# ==================== REFER & EARN FLOW ====================

async def config_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the referral bonus. Zero switches Refer & Earn off."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    from services import referrals
    from utils import create_cancel_keyboard, format_price
    current = await asyncio.to_thread(referrals.bonus_amount_sync)

    state = ("OFF - the menu button is hidden" if current <= 0
             else f"ON - {format_price(current)} per referral")

    await query.edit_message_text(
        "👥 Refer & Earn\n\n"
        f"Currently: {state}\n\n"
        "The bonus is paid to the referrer when the person they invited "
        "completes their first purchase - so the store has taken real money "
        "before it gives any away.\n\n"
        "Send the amount in USD (for example 1.00), or 0 to switch it off:",
        reply_markup=create_cancel_keyboard()
    )
    return REFERRAL_BONUS


async def referral_bonus_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the referral bonus."""
    from services import referrals, store_content
    from utils import (create_admin_settings_menu_keyboard, create_cancel_keyboard,
                       money_or_none, format_price)

    amount = money_or_none(update.message.text)
    if amount is None or amount < 0:
        await update.message.reply_text(
            "❌ Please send a number, for example 1.00 (or 0 to switch it off):",
            reply_markup=create_cancel_keyboard()
        )
        return REFERRAL_BONUS

    saved = await asyncio.to_thread(referrals.set_bonus_sync, amount,
                                    update.effective_user.id)
    store_content.set_referral_bonus_cache(saved > 0)

    await update.message.reply_text(
        (f"✅ Refer & Earn is on. Referrers get {format_price(saved)} once the "
         "person they invited completes their first purchase."
         if saved > 0 else
         "✅ Refer & Earn is off. The menu button is hidden."),
        reply_markup=create_admin_settings_menu_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


# ==================== TERMS & FAQ FLOW ====================

# Telegram rejects a message over 4096 characters, and the terms are shown
# on their own screen with a heading above them.
_MAX_TERMS = 3500


# Which page an admin is editing, and how each is described.
_PAGES = {
    "admin_terms": ("terms", "📜 Terms & Conditions",
                    "Your refund policy, warranty period and delivery times. "
                    "Stating the rules up front is what stops most disputes "
                    "becoming arguments."),
    "admin_faq": ("faq", "❓ Frequently Asked Questions",
                  "The questions support answers over and over: how delivery "
                  "works, what to do if a key fails, how long a top-up takes."),
}


async def config_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit one of the two pages behind Terms & FAQ."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    page, heading, blurb = _PAGES.get(query.data, _PAGES["admin_terms"])
    context.user_data['terms_page'] = page

    from services import store_content
    current = await asyncio.to_thread(store_content.get_page_sync, page)

    from utils import create_cancel_keyboard
    await query.edit_message_text(
        f"{heading}\n\n{blurb}\n\n"
        f"Current:\n{current or '(not set)'}\n\n"
        "Send the new text, or 'clear' to remove it:",
        reply_markup=create_cancel_keyboard()
    )
    return TERMS_TEXT


async def terms_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the Terms & FAQ text."""
    text = (update.message.text or "").strip()
    admin_id = update.effective_user.id

    from utils import create_admin_settings_menu_keyboard, create_cancel_keyboard

    if len(text) > _MAX_TERMS:
        await update.message.reply_text(
            f"❌ That is {len(text)} characters; please keep the terms under "
            f"{_MAX_TERMS} so they fit in one Telegram message.",
            reply_markup=create_cancel_keyboard()
        )
        return TERMS_TEXT

    from services import store_content
    if text.lower() == 'clear':
        text = ""

    page = context.user_data.get('terms_page', 'terms')
    saved = await asyncio.to_thread(store_content.set_page_sync, page, text, admin_id)
    label = "FAQ" if page == "faq" else "Terms & Conditions"

    await update.message.reply_text(
        (f"✅ {label} updated. Customers can read it under Terms & FAQ."
         if saved else
         f"✅ {label} cleared."
         + ("" if store_content.has_terms()
            else " The menu button is hidden again.")),
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
