"""Dispute handling for orders.

Same asyncio.to_thread refactor as user_handlers.py/payment_handlers.py:
each "with get_db_session()" block's query work runs in a nested _sync()
closure off the event loop, returning only plain data, with the final
Telegram API call left on the event loop.
"""

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import get_db_session, User, Order, Dispute, DisputeStatus
from utils import (
    format_price, format_datetime, notify_admin, create_cancel_keyboard,
    is_admin, check_user_banned_async, log_admin_action
)
from datetime import datetime

logger = logging.getLogger(__name__)

# Conversation states
DISPUTE_REASON = 0


async def open_dispute_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start dispute opening flow - ask for reason."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if await check_user_banned_async(user_id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return ConversationHandler.END

    # Extract order_id from callback data (format: open_dispute_<id>)
    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return ConversationHandler.END

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                return "no_user"

            order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
            if not order:
                return "no_order"

            # Check if dispute already exists
            if order.dispute_status != DisputeStatus.NIL:
                return order.dispute_status.value

            return None

    result = await asyncio.to_thread(_sync)

    if result == "no_user":
        await query.edit_message_text("❌ User not found.")
        return ConversationHandler.END
    if result == "no_order":
        await query.edit_message_text("❌ Order not found.")
        return ConversationHandler.END
    if result is not None:
        await query.edit_message_text(
            f"⚠️ This order already has a dispute with status: {result}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="order_history")]])
        )
        return ConversationHandler.END

    # Store order_id in context for later use
    context.user_data['dispute_order_id'] = order_id

    await query.edit_message_text(
        f"🚨 Open Dispute for Order #{order_id}\n\n"
        f"Please describe the issue with your order.\n"
        f"Be as detailed as possible to help us resolve this quickly:",
        reply_markup=create_cancel_keyboard()
    )

    return DISPUTE_REASON


async def dispute_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive dispute reason and create dispute."""
    reason = update.message.text
    order_id = context.user_data.get('dispute_order_id')
    user_id = update.effective_user.id

    if not order_id:
        await update.message.reply_text("❌ Session expired. Please try again.")
        return ConversationHandler.END

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                return "no_user", None

            order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
            if not order:
                return "no_order", None

            # Create dispute
            dispute = Dispute(
                order_id=order.id,
                user_id=user.id,
                reason=reason,
                status=DisputeStatus.OPENED,
                created_at=datetime.utcnow()
            )
            session.add(dispute)

            # Update order dispute status
            order.dispute_status = DisputeStatus.OPENED
            session.commit()

            return "ok", (order.total_amount, dispute.id)

    status, data = await asyncio.to_thread(_sync)

    if status == "no_user":
        await update.message.reply_text("❌ User not found.")
        return ConversationHandler.END
    if status == "no_order":
        await update.message.reply_text("❌ Order not found.")
        return ConversationHandler.END

    order_total, dispute_id = data
    username = update.effective_user.username or "No username"

    # Notify admin about new dispute
    admin_message = f"""🚨 NEW DISPUTE OPENED

Order ID: #{order_id}
User: @{username} (ID: {user_id})
Amount: {format_price(order_total)}

Reason:
{reason}

Use /admin to manage disputes."""

    await notify_admin(context, admin_message)

    # Confirm to user
    keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="order_history")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Dispute opened successfully!\n\n"
        f"Your dispute for Order #{order_id} has been submitted.\n"
        f"Our admin team will review it and contact you soon.\n\n"
        f"Dispute ID: #{dispute_id}",
        reply_markup=reply_markup
    )

    # Clear context
    context.user_data.pop('dispute_order_id', None)

    return ConversationHandler.END


async def dispute_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel dispute creation."""
    query = update.callback_query
    await query.answer()

    # Clear context
    context.user_data.pop('dispute_order_id', None)

    keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="order_history")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "❌ Dispute opening cancelled.",
        reply_markup=reply_markup
    )

    return ConversationHandler.END


# ============================================================================
# ADMIN DISPUTE HANDLERS
# ============================================================================

async def admin_view_disputes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all open disputes for admin."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    def _sync():
        with get_db_session() as session:
            # Get all open disputes
            disputes = session.query(Dispute).filter_by(status=DisputeStatus.OPENED).order_by(Dispute.created_at.desc()).all()

            rows = []
            for dispute in disputes:
                order = session.query(Order).filter_by(id=dispute.order_id).first()
                user = session.query(User).filter_by(id=dispute.user_id).first()
                if not order or not user:
                    continue
                rows.append((dispute.id, order.id, user.username))
            return rows

    rows = await asyncio.to_thread(_sync)

    if not rows:
        keyboard = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ No open disputes at the moment.",
            reply_markup=reply_markup
        )
        return

    # Build keyboard with dispute buttons
    keyboard = []
    for dispute_id, order_id, username in rows:
        button_text = f"🚨 Dispute #{dispute_id} | Order #{order_id} | @{username or 'No username'}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"admin_dispute_detail_{dispute_id}")])

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = f"🚨 Open Disputes ({len(rows)})\n\nClick on a dispute to view details and resolve:"

    await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_dispute_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show dispute details with resolution buttons for admin."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # Extract dispute_id from callback data (format: admin_dispute_detail_<id>)
    try:
        dispute_id = int(query.data.split("_")[3])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            dispute = session.query(Dispute).filter_by(id=dispute_id).first()

            if not dispute:
                return "no_dispute", None

            order = session.query(Order).filter_by(id=dispute.order_id).first()
            user = session.query(User).filter_by(id=dispute.user_id).first()

            if not order or not user:
                return "no_related", None

            # Get order items for display
            from database import OrderItem
            order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

            items_text = ""
            for item in order_items:
                product_name = item.product.name if item.product else "Deleted product"
                items_text += f"  📦 {product_name} (x{item.quantity}) - {format_price(item.price * item.quantity)}\n"

            # Build message
            status_emoji = {
                DisputeStatus.OPENED: "🚨",
                DisputeStatus.RESOLVED: "✔️",
                DisputeStatus.NIL: "❓"
            }.get(dispute.status, "❓")

            message = f"""🚨 Dispute Details

Dispute ID: #{dispute.id}
Status: {status_emoji} {dispute.status.value}

📋 Order Information:
Order ID: #{order.id}
Order Status: {order.status.value}
Total Amount: {format_price(order.total_amount)}
Date: {format_datetime(order.created_at)}

👤 User Information:
Username: @{user.username or 'No username'}
Telegram ID: {user.telegram_id}

📦 Order Items:
{items_text}
📝 Dispute Reason:
{dispute.reason}

Opened: {format_datetime(dispute.created_at)}"""

            if dispute.admin_notes:
                message += f"\n\n📌 Admin Notes:\n{dispute.admin_notes}"

            if dispute.resolved_at:
                message += f"\n\nResolved: {format_datetime(dispute.resolved_at)}"

            # Build keyboard
            keyboard = []

            if dispute.status == DisputeStatus.OPENED:
                keyboard.append([InlineKeyboardButton("✅ Resolve Dispute", callback_data=f"resolve_dispute_{dispute.id}")])

            keyboard.append([InlineKeyboardButton("🔙 Back to Disputes", callback_data="admin_view_disputes")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            return "ok", (message, reply_markup)

    status, payload = await asyncio.to_thread(_sync)

    if status == "no_dispute":
        await query.edit_message_text("❌ Dispute not found.")
        return
    if status == "no_related":
        await query.edit_message_text("❌ Related order or user is missing.")
        return

    message, reply_markup = payload
    await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_resolve_dispute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve a dispute - mark as resolved and notify user."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # Extract dispute_id from callback data (format: resolve_dispute_<id>)
    try:
        dispute_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            dispute = session.query(Dispute).filter_by(id=dispute_id).first()

            if not dispute:
                return "no_dispute", None

            if dispute.status == DisputeStatus.RESOLVED:
                # query.answer() already fired unconditionally above; a second
                # call for the same callback query raises, so use
                # edit_message_text here instead (same fix as the other admin
                # handlers that hit this).
                return "already_resolved", None

            order = session.query(Order).filter_by(id=dispute.order_id).first()
            user = session.query(User).filter_by(id=dispute.user_id).first()

            if not order or not user:
                return "no_related", None

            # Update dispute status
            dispute.status = DisputeStatus.RESOLVED
            dispute.resolved_at = datetime.utcnow()

            # Update order dispute status
            order.dispute_status = DisputeStatus.RESOLVED

            log_admin_action(session, update.effective_user.id, "resolve_dispute",
                              target_type="dispute", target_id=dispute.id,
                              details=f"order_id={order.id}")
            session.commit()

            return "ok", (user.telegram_id, order.id)

    status, payload = await asyncio.to_thread(_sync)

    if status == "no_dispute":
        await query.edit_message_text("❌ Dispute not found.")
        return
    if status == "already_resolved":
        await query.edit_message_text("⚠️ This dispute is already resolved.")
        return
    if status == "no_related":
        await query.edit_message_text("❌ Related order or user is missing.")
        return

    # Get user telegram_id before closing session
    user_telegram_id, order_id = payload

    # Notify user about dispute resolution
    try:
        user_keyboard = [[InlineKeyboardButton("🏠 Home", callback_data="main_menu")]]
        user_reply_markup = InlineKeyboardMarkup(user_keyboard)

        await context.bot.send_message(
            chat_id=user_telegram_id,
            text=f"✅ Your dispute for Order #{order_id} has been resolved.\n\n"
                 f"Thank you for your patience. If you have any further questions, please contact support.",
            reply_markup=user_reply_markup
        )
    except Exception:
        logger.exception("Error notifying user about dispute resolution")

    # Confirm to admin
    keyboard = [[InlineKeyboardButton("🔙 Back to Disputes", callback_data="admin_view_disputes")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ Dispute #{dispute_id} has been resolved!\n\n"
        f"User has been notified.",
        reply_markup=reply_markup
    )
