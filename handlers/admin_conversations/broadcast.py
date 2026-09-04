"""Text and image broadcast conversation flows."""

import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session
)
from utils import is_admin, broadcast

logger = logging.getLogger(__name__)

# Conversation states
BROADCAST_TEXT, BROADCAST_IMAGE = range(2)


# ==================== BROADCAST FLOW ====================

async def broadcast_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start text-only broadcast flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    from utils import create_cancel_keyboard
    await query.edit_message_text(
        "💬 Text Only Broadcast\n\n"
        "Please send the message you want to broadcast to all users:",
        reply_markup=create_cancel_keyboard()
    )
    return BROADCAST_TEXT


async def broadcast_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast text message and send to all users."""
    from database import User
    from utils import create_admin_broadcast_menu_keyboard

    message_text = update.message.text

    def _sync():
        # Get all users (banned users must not receive broadcasts)
        with get_db_session() as session:
            users = session.query(User).filter_by(is_banned=False).all()
            return [u.telegram_id for u in users]

    recipient_ids = await asyncio.to_thread(_sync)

    if not recipient_ids:
        await update.message.reply_text(
            "❌ No users found in the database.",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    # utils.broadcast paces the run, waits out Telegram's flood control
    # rather than counting it as a failure, and splits a message too long
    # for one send.
    result = await broadcast(context.bot, recipient_ids, message_text)

    result_msg = "✅ Broadcast Complete!\n\n📊 Results:\n" + result.summary()

    await update.message.reply_text(
        result_msg,
        reply_markup=create_admin_broadcast_menu_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def broadcast_image_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start image+text broadcast flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    from utils import create_cancel_keyboard
    await query.edit_message_text(
        "🖼 Image + Text Broadcast\n\n"
        "Step 1/2: Please send the image you want to broadcast:",
        reply_markup=create_cancel_keyboard()
    )
    return BROADCAST_IMAGE


async def broadcast_image_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast image and ask for caption text."""
    from utils import create_cancel_keyboard

    # Store the photo file_id
    context.user_data['broadcast_image_file_id'] = update.message.photo[-1].file_id

    await update.message.reply_text(
        "✅ Image received!\n\n"
        "Step 2/2: Please send the text/caption for this broadcast:",
        reply_markup=create_cancel_keyboard()
    )
    return BROADCAST_TEXT


async def broadcast_image_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle broadcast caption text and send image+text to all users."""
    from database import User
    from utils import create_admin_broadcast_menu_keyboard

    caption_text = update.message.text
    image_file_id = context.user_data.get('broadcast_image_file_id')

    if not image_file_id:
        await update.message.reply_text(
            "❌ Error: Image not found. Please try again.",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    def _sync():
        # Get all users (banned users must not receive broadcasts)
        with get_db_session() as session:
            users = session.query(User).filter_by(is_banned=False).all()
            return [u.telegram_id for u in users]

    recipient_ids = await asyncio.to_thread(_sync)

    if not recipient_ids:
        await update.message.reply_text(
            "❌ No users found in the database.",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    # The image goes first, then the text, to the same chat. A recipient
    # who fails at either step is counted once, by whatever stopped them.
    result = await broadcast(context.bot, recipient_ids, caption_text,
                             photo=image_file_id)

    result_msg = "✅ Broadcast Complete!\n\n📊 Results:\n" + result.summary()

    await update.message.reply_text(
        result_msg,
        reply_markup=create_admin_broadcast_menu_keyboard()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel broadcast flow."""
    from utils import create_admin_broadcast_menu_keyboard

    # Handle both callback query and message
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Broadcast cancelled.",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Broadcast cancelled.",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END
