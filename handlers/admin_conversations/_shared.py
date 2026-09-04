"""Generic cancel handler shared across the product/category/subcategory
edit conversations (registered as their ConversationHandler fallback)."""

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from utils import create_admin_category_menu_keyboard

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic cancel handler for conversations."""

    # Handle both callback queries and messages
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Operation cancelled.",
            reply_markup=create_admin_category_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=create_admin_category_menu_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END
