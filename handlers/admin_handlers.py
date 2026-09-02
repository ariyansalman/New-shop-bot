"""Admin panel command and callback handlers.

Same asyncio.to_thread refactor as the user-facing handler modules: every
"with get_db_session()" block's query work runs in a nested _sync()
closure off the event loop, returning only plain data, with the final
Telegram API call left on the event loop. Blocking here still freezes
every other user's handler while it runs, even though only the store
owner triggers these.

_build_user_detail_sync and _restock_keys_sync are pulled out to module
level because the original code had that exact block duplicated 2-3 times
(view/ban/unban user detail; file-upload/paste restock) - mechanical dedup
of already-identical code, not a behavior change.
"""

import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
from database import (
    get_db_session, User, Category, Subcategory, Product, ProductKey,
    Order, OrderItem, Transaction, AdminActionLog,
    ProductType, OrderStatus, TransactionStatus
)
from utils import (
    is_admin, admin_only, format_price, to_money, log_admin_action,
    create_admin_product_menu_keyboard,
    create_admin_category_menu_keyboard, create_admin_user_menu_keyboard,
    create_admin_order_menu_keyboard, create_admin_settings_menu_keyboard,
    create_admin_broadcast_menu_keyboard, parse_keys_from_text, clear_ban_cache
)
from telegram.ext import ConversationHandler

# Conversation states for restock keys
WAITING_FOR_KEYS = 1

# Upper bound for uploaded key files (1 MB)
MAX_KEYS_FILE_BYTES = 1024 * 1024


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command - show the Admin Panel.

    The panel itself lives in handlers/admin_panel.py; this only opens it,
    so /admin and the main-menu button show the same thing.
    """
    from handlers.admin_panel import admin_panel_main_markup

    await update.message.reply_text(
        "👑 ADMIN PANEL\n━━━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_panel_main_markup()
    )


async def admin_action_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the most recent audit-logged admin actions (ban, refund, restock, ...)."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    def _sync():
        with get_db_session() as session:
            entries = session.query(AdminActionLog).order_by(
                AdminActionLog.created_at.desc()
            ).limit(20).all()

            if not entries:
                return "📜 Admin Action Log\n\nNo actions recorded yet."

            lines = ["📜 Admin Action Log (last 20)\n"]
            for entry in entries:
                when = entry.created_at.strftime('%Y-%m-%d %H:%M')
                target = f" {entry.target_type}#{entry.target_id}" if entry.target_type else ""
                line = f"{when} · admin {entry.admin_telegram_id} · {entry.action}{target}"
                if entry.details:
                    line += f" ({entry.details})"
                lines.append(line)
            return "\n".join(lines)

    message = await asyncio.to_thread(_sync)

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_menu")]]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_restock_keys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin restock keys button - show product selection."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    def _sync():
        with get_db_session() as session:
            # Get all KEY type products
            products = session.query(Product).filter_by(product_type=ProductType.KEY).all()
            return [(p.id, p.name, p.stock_count) for p in products]

    products = await asyncio.to_thread(_sync)

    if not products:
        await query.edit_message_text(
            "❌ No KEY products found. Please create a product first.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        return

    # Build product selection keyboard
    keyboard = []
    for product_id, name, stock_count in products[:10]:  # Show first 10
        keyboard.append([
            InlineKeyboardButton(
                f"📦 {name} (Stock: {stock_count})",
                callback_data=f"select_product_{product_id}"
            )
        ])

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])

    await query.edit_message_text(
        "🔄 Select a product to restock:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_select_product_restock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection for restocking."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # Extract product ID from callback data. Every other handler in this file
    # guards this parse; this one didn't, so malformed data raised inside the
    # handler and - because query.answer() has already fired above - the
    # global error handler could not report it either.
    try:
        product_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return ConversationHandler.END

    # Store product ID in context for later use
    context.user_data['restock_product_id'] = product_id

    def _sync():
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=product_id).first()
            if not product:
                return None
            return product.name, product.stock_count

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ Product not found.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        return

    product_name, stock_count = result

    message = f"""🔄 Restocking: {product_name}
Current Stock: {stock_count}

📤 Upload a .txt file with keys (one per line)
OR
✍️ Paste keys directly (one per line)

Example:
KEY1-XXXX-XXXX-XXXX
KEY2-XXXX-XXXX-XXXX
KEY3-XXXX-XXXX-XXXX"""

    # Create keyboard with cancel button
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_restock")]]

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Return state to wait for keys
    return WAITING_FOR_KEYS


async def admin_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin products menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "📦 Product Management\n\nSelect an option:",
            reply_markup=create_admin_product_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_manage_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin category management menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "📁 Category Management\n\nSelect an option:",
            reply_markup=create_admin_category_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_view_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of all categories and subcategories."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    def _sync():
        with get_db_session() as session:
            categories = session.query(Category).all()

            if not categories:
                return None

            message = "📁 Categories & Subcategories:\n\n"

            for cat in categories:
                message += f"📦 {cat.name} (ID: #{cat.id})\n"
                if cat.description:
                    message += f"   {cat.description}\n"

                subcategories = session.query(Subcategory).filter_by(category_id=cat.id).all()
                if subcategories:
                    for subcat in subcategories:
                        message += f"   └─ {subcat.name} (ID: #{subcat.id})\n"

                message += "\n"

            return message

    message = await asyncio.to_thread(_sync)

    if message is None:
        await query.edit_message_text("📁 No categories found.")
        return

    await query.edit_message_text(message, reply_markup=create_admin_category_menu_keyboard())


async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin users menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "👥 User Management\n\nSelect an option:",
            reply_markup=create_admin_user_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin orders menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "🛍 Order Management\n\nSelect an option:",
            reply_markup=create_admin_order_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin settings menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "⚙️ Store Settings\n\nSelect an option:",
            reply_markup=create_admin_settings_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin broadcast menu."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        await query.edit_message_text(
            "📢 Broadcast Messages\n\nSelect an option:",
            reply_markup=create_admin_broadcast_menu_keyboard()
        )
    except Exception:
        # Message is already showing the same content, ignore
        pass


async def admin_view_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of users."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    def _sync():
        with get_db_session() as session:
            all_users = session.query(User).order_by(User.created_at.desc()).all()
            return [(u.id, u.telegram_id, u.username, u.wallet_balance, u.is_banned) for u in all_users]

    users_data = await asyncio.to_thread(_sync)

    if not users_data:
        await query.edit_message_text(
            "👥 No users found.",
            reply_markup=create_admin_user_menu_keyboard()
        )
        return

    # Pagination settings
    items_per_page = 5
    total_pages = (len(users_data) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_users = users_data[start_idx:end_idx]

    # Build user selection keyboard
    keyboard = []
    for user_id, telegram_id, username, wallet_balance, is_banned in page_users:
        status_icon = "🚫" if is_banned else "✅"
        username_display = f"@{username}" if username else f"ID:{telegram_id}"
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {username_display} - {format_price(wallet_balance)}",
                callback_data=f"view_user_{user_id}"
            )
        ])

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_users_page_{page-1}"))
        pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_users_page_{page+1}"))
        keyboard.append(pagination_row)

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_users")])

    await query.edit_message_text(
        "👥 User List\n\nSelect a user to view details:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def _build_user_detail_sync(session, user):
    """Build the user detail message + Ban/Unban keyboard.

    Shared by admin_user_detail_callback, admin_ban_user_callback and
    admin_unban_user_callback (all three rendered this exact view in the
    original code) - must be called with `session` still open, since it
    runs queries against `user`'s relations.
    """
    orders_count = session.query(Order).filter_by(user_id=user.id).count()
    total_spent = session.query(Order).filter_by(user_id=user.id, status=OrderStatus.COMPLETED).with_entities(
        func.sum(Order.total_amount)
    ).scalar() or 0

    status = "🚫 Banned" if user.is_banned else "✅ Active"
    username_display = f"@{user.username}" if user.username else "N/A"

    message = "👤 User Details\n\n"
    message += f"Telegram ID: {user.telegram_id}\n"
    message += f"Username: {username_display}\n"
    message += f"Balance: {format_price(user.wallet_balance)}\n"
    message += f"Status: {status}\n"
    message += f"Total Orders: {orders_count}\n"
    message += f"Total Spent: {format_price(total_spent)}\n"
    message += f"Joined: {user.created_at.strftime('%Y-%m-%d %H:%M')}\n"

    keyboard = []
    if user.is_banned:
        keyboard.append([InlineKeyboardButton("✅ Unban User", callback_data=f"unban_user_{user.id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 Ban User", callback_data=f"ban_user_{user.id}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to User List", callback_data="admin_view_users")])

    return message, InlineKeyboardMarkup(keyboard)


async def admin_user_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual user details with Ban/Unban button."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # (Dead branch removed: this handler is only routed for "^view_user_", so
    # the pagination check could never fire, and calling the list handler from
    # here would have answered the callback query a second time.)
    try:
        user_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(id=user_id).first()
            if not user:
                return None
            return _build_user_detail_sync(session, user)

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ User not found.",
            reply_markup=create_admin_user_menu_keyboard()
        )
        return

    message, reply_markup = result
    await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle banning a user."""
    query = update.callback_query

    # Authorize BEFORE answering, otherwise a non-admin gets a "success" popup
    # and the second answer() call raises.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer("✅ User banned successfully!", show_alert=True)

    # Extract user ID from callback data
    try:
        user_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(id=user_id).first()

            if not user:
                return None

            # Store telegram_id before committing
            telegram_id = user.telegram_id

            user.is_banned = True
            log_admin_action(session, update.effective_user.id, "ban_user",
                              target_type="user", target_id=user.id,
                              details=f"telegram_id={telegram_id}")
            session.commit()

            # Clear ban cache for this user
            clear_ban_cache(telegram_id)

            # Refresh user details page - get updated data
            user = session.query(User).filter_by(id=user_id).first()
            return _build_user_detail_sync(session, user)

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ User not found.",
            reply_markup=create_admin_user_menu_keyboard()
        )
        return

    message, reply_markup = result
    await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_unban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unbanning a user."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer("✅ User unbanned successfully!", show_alert=True)

    # Extract user ID from callback data
    try:
        user_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(id=user_id).first()

            if not user:
                return None

            # Store telegram_id before committing
            telegram_id = user.telegram_id

            user.is_banned = False
            log_admin_action(session, update.effective_user.id, "unban_user",
                              target_type="user", target_id=user.id,
                              details=f"telegram_id={telegram_id}")
            session.commit()

            # Clear ban cache for this user
            clear_ban_cache(telegram_id)

            # Refresh user details page - get updated data
            user = session.query(User).filter_by(id=user_id).first()
            return _build_user_detail_sync(session, user)

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ User not found.",
            reply_markup=create_admin_user_menu_keyboard()
        )
        return

    message, reply_markup = result
    await query.edit_message_text(message, reply_markup=reply_markup)


async def admin_view_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show paginated list of recent orders with management buttons."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    def _sync():
        with get_db_session() as session:
            all_orders = session.query(Order).order_by(Order.created_at.desc()).all()

            rows = []
            for order in all_orders:
                user = session.query(User).filter_by(id=order.user_id).first()
                username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"
                rows.append((order.id, order.status, order.total_amount, username))
            return rows

    rows = await asyncio.to_thread(_sync)

    if not rows:
        await query.edit_message_text(
            "🛍 No orders found.",
            reply_markup=create_admin_order_menu_keyboard()
        )
        return

    # Pagination settings
    orders_per_page = 5
    total_pages = (len(rows) + orders_per_page - 1) // orders_per_page
    start_idx = page * orders_per_page
    end_idx = start_idx + orders_per_page
    page_rows = rows[start_idx:end_idx]

    # Build message
    message = f"🛍 Recent Orders (Page {page + 1}/{total_pages}):\n\n"

    # Build keyboard with order buttons
    keyboard = []

    for order_id, status, total_amount, username in page_rows:
        # Format status emoji
        status_emoji = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED: "✅",
            OrderStatus.CANCELLED: "❌"
        }.get(status, "❓")

        # Button text: Order #ID | User | Status | Amount
        button_text = f"{status_emoji} Order #{order_id} | @{username} | {format_price(total_amount)}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_order_{order_id}")])

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_view_orders_page_{page-1}"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_view_orders_page_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_orders")])

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def _restock_keys_sync(product_id, keys, admin_telegram_id):
    """Add `keys` to `product_id`'s stock, skipping duplicates.

    Shared by handle_restock_keys_file and handle_restock_keys_paste (both
    had this exact block in the original code). Returns None if the
    product doesn't exist, else (product_name, added_count, skipped_count,
    new_stock_count).
    """
    with get_db_session() as session:
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            return None

        # Skip keys that already exist for this product, so a re-uploaded file
        # cannot inflate stock with duplicates that would be delivered twice.
        existing = {
            k[0] for k in session.query(ProductKey.key_value)
            .filter_by(product_id=product.id).all()
        }
        added_count = 0
        skipped = 0
        for key_value in keys:
            if key_value in existing:
                skipped += 1
                continue
            existing.add(key_value)
            session.add(ProductKey(
                product_id=product.id,
                key_value=key_value,
                is_sold=False
            ))
            added_count += 1

        # Keep stock in sync with the real unsold-key count. The newly added
        # keys above are already in this count: session.query() autoflushes
        # pending session.add() calls before running, so they're already in
        # the DB by the time count() runs - adding + added_count on top of
        # that used to double-count every restock.
        product.stock_count = session.query(ProductKey).filter_by(
            product_id=product.id, is_sold=False
        ).count()
        log_admin_action(session, admin_telegram_id, "restock_keys",
                          target_type="product", target_id=product.id,
                          details=f"added={added_count}, skipped_duplicates={skipped}")
        session.commit()

        return product.name, added_count, skipped, product.stock_count


async def handle_restock_keys_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file upload for restocking keys."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    # Get the uploaded file
    document = update.message.document

    if not document:
        await update.message.reply_text("❌ Please upload a text file with keys. Try again or /cancel")
        return WAITING_FOR_KEYS

    # Reject oversized uploads before downloading them into memory.
    if document.file_size and document.file_size > MAX_KEYS_FILE_BYTES:
        await update.message.reply_text(
            f"❌ File is too large (max {MAX_KEYS_FILE_BYTES // 1024} KB). Try again or /cancel"
        )
        return WAITING_FOR_KEYS

    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        # A non-UTF-8 file used to raise UnicodeDecodeError and crash the handler.
        text = bytes(file_content).decode('utf-8', errors='replace')
    except Exception as e:
        await update.message.reply_text(f"❌ Could not read the file: {e}\n\nTry again or /cancel")
        return WAITING_FOR_KEYS

    keys = parse_keys_from_text(text)

    if not keys:
        await update.message.reply_text("❌ No keys found in file. Try again or /cancel")
        return WAITING_FOR_KEYS

    # Get product ID from context (should be set earlier)
    product_id = context.user_data.get('restock_product_id')

    if not product_id:
        await update.message.reply_text("❌ Error: Product not selected. Please start over.")
        return ConversationHandler.END

    result = await asyncio.to_thread(_restock_keys_sync, product_id, keys, update.effective_user.id)

    if result is None:
        await update.message.reply_text("❌ Product not found.")
        context.user_data.pop('restock_product_id', None)
        return ConversationHandler.END

    product_name, added_count, skipped, new_stock = result

    # Create keyboard with options
    keyboard = [
        [InlineKeyboardButton("🔄 Restock More Keys", callback_data="admin_restock_keys")],
        [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    summary = f"✅ Successfully added {added_count} keys to {product_name}!"
    if skipped:
        summary += f"\n⏭ Skipped {skipped} duplicate key(s)."
    summary += f"\nNew stock count: {new_stock}"

    await update.message.reply_text(summary, reply_markup=reply_markup)

    # Clear restock_product_id from context
    context.user_data.pop('restock_product_id', None)

    return ConversationHandler.END


async def handle_restock_keys_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pasted keys for restocking."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return ConversationHandler.END

    # Parse keys from message text
    keys = parse_keys_from_text(update.message.text)

    if not keys:
        await update.message.reply_text("❌ No keys found. Please paste keys (one per line). Try again or /cancel")
        return WAITING_FOR_KEYS

    # Get product ID from context (should be set earlier)
    product_id = context.user_data.get('restock_product_id')

    if not product_id:
        await update.message.reply_text("❌ Error: Product not selected. Please start over.")
        return ConversationHandler.END

    result = await asyncio.to_thread(_restock_keys_sync, product_id, keys, update.effective_user.id)

    if result is None:
        await update.message.reply_text("❌ Product not found.")
        context.user_data.pop('restock_product_id', None)
        return ConversationHandler.END

    product_name, added_count, skipped, new_stock = result

    # Create keyboard with options
    keyboard = [
        [InlineKeyboardButton("🔄 Restock More Keys", callback_data="admin_restock_keys")],
        [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    summary = f"✅ Successfully added {added_count} keys to {product_name}!"
    if skipped:
        summary += f"\n⏭ Skipped {skipped} duplicate key(s)."
    summary += f"\nNew stock count: {new_stock}"

    await update.message.reply_text(summary, reply_markup=reply_markup)

    # Clear restock_product_id from context
    context.user_data.pop('restock_product_id', None)

    return ConversationHandler.END


async def _render_order_detail(query, order_id: int):
    """Render the order detail view onto an existing message.

    Split out of the callback so that handlers which have already answered the
    callback query can refresh the view without calling query.answer() twice
    (Telegram rejects the second answer).
    """
    def _sync():
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                return None

            user = session.query(User).filter_by(id=order.user_id).first()
            order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

            status_emoji = {
                OrderStatus.PROCESSING: "⏳",
                OrderStatus.COMPLETED: "✅",
                OrderStatus.CANCELLED: "❌"
            }.get(order.status, "❓")

            username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"
            message = "📋 Order Details\n\n"
            message += f"Order ID: #{order.id}\n"
            message += f"Status: {status_emoji} {order.status.value}\n"
            message += f"User: @{username} ({user.telegram_id if user else 'Unknown'})\n"
            message += f"Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            message += f"Total: {format_price(order.total_amount)}\n\n"

            message += "📦 Items:\n"
            for item in order_items:
                product = session.query(Product).filter_by(id=item.product_id).first()
                product_name = product.name if product else "Unknown Product"
                message += f"• {product_name} x{item.quantity} = {format_price(item.price * item.quantity)}\n"

                if item.delivered_asset:
                    if product and product.product_type == ProductType.KEY:
                        message += f"  🔐 Keys:\n{item.delivered_asset}\n"
                    elif product and product.product_type == ProductType.FILE:
                        message += f"  🔗 Download: {item.delivered_asset}\n"
                    message += "\n"

            keyboard = []
            if order.status == OrderStatus.PROCESSING:
                keyboard.append([InlineKeyboardButton("✅ Mark as Completed", callback_data=f"complete_order_{order.id}")])
                keyboard.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order_{order.id}")])
            elif order.status == OrderStatus.CANCELLED:
                keyboard.append([InlineKeyboardButton("🔄 Reactivate Order", callback_data=f"reactivate_order_{order.id}")])

            keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_view_orders")])

            return message, InlineKeyboardMarkup(keyboard)

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ Order not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_view_orders")]])
        )
        return

    message, reply_markup = result
    try:
        await query.edit_message_text(message, reply_markup=reply_markup)
    except Exception:
        # Same content as before -> Telegram returns "message is not modified"
        pass


async def admin_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show individual order details with management buttons."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    await _render_order_detail(query, order_id)


async def admin_reactivate_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactivate a cancelled order.

    The "Reactivate Order" button existed in the UI but had no registered
    handler, so it silently did nothing.
    """
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    def _sync():
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()
            if not order:
                return "not_found"
            if order.status != OrderStatus.CANCELLED:
                return "not_cancelled"

            user = session.query(User).filter_by(id=order.user_id).first()
            if not user or user.wallet_balance < order.total_amount:
                return "insufficient"

            # Take the refunded money back out and restore the order.
            user.wallet_balance = to_money(user.wallet_balance - order.total_amount)
            order.status = OrderStatus.PROCESSING
            log_admin_action(session, update.effective_user.id, "reactivate_order",
                              target_type="order", target_id=order.id,
                              details=f"amount={order.total_amount}")
            session.commit()
            return "ok"

    result = await asyncio.to_thread(_sync)

    if result == "not_found":
        await query.answer("❌ Order not found.", show_alert=True)
        return
    if result == "not_cancelled":
        await query.answer("⚠️ Only cancelled orders can be reactivated.", show_alert=True)
        return
    if result == "insufficient":
        await query.answer(
            "❌ Cannot reactivate: the refund is no longer available in the user's wallet.",
            show_alert=True
        )
        return

    await query.answer("✅ Order reactivated.", show_alert=True)
    await _render_order_detail(query, order_id)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer the non-interactive 'Page x/y' buttons so they stop spinning."""
    await update.callback_query.answer()


async def admin_complete_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark an order as completed."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                return "not_found"

            if order.status == OrderStatus.COMPLETED:
                return "already"

            order.status = OrderStatus.COMPLETED
            order.completed_at = datetime.utcnow()
            log_admin_action(session, update.effective_user.id, "complete_order",
                              target_type="order", target_id=order.id)
            session.commit()
            return "ok"

    result = await asyncio.to_thread(_sync)

    if result == "not_found":
        await query.edit_message_text("❌ Order not found.")
        return
    if result == "already":
        await query.edit_message_text("ℹ️ Order is already completed.")
        return

    # NOTE: query.answer() was already called at the top of this handler;
    # calling it twice raises. Render the refreshed detail view instead.
    await _render_order_detail(query, order_id)


async def _render_pending_txn_menu(query, action: str):
    """Render the pending-transaction list for confirm/cancel actions."""
    is_confirm = action == "confirm"
    prefix = "confirm_payment_" if is_confirm else "cancel_payment_"

    def _sync():
        with get_db_session() as session:
            transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING
            ).order_by(Transaction.created_at.desc()).all()

            rows = []
            for txn in transactions:
                user = session.query(User).filter_by(id=txn.user_id).first()
                username = user.username if user and user.username else f"ID:{user.telegram_id if user else 'Unknown'}"
                rows.append((txn.id, username, txn.amount, txn.payment_method.value))
            return rows

    rows = await asyncio.to_thread(_sync)

    back = [[InlineKeyboardButton("🔙 Back to Orders", callback_data="admin_orders")]]

    if not rows:
        text = "✅ No pending payments to confirm." if is_confirm else "✅ No pending payments to cancel."
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back))
        except Exception:
            pass
        return

    keyboard = []
    for txn_id, username, amount, payment_method_value in rows:
        payment_method = payment_method_value.replace('_', ' ').title()
        button_text = f"⏳ Txn #{txn_id} | @{username} | {format_price(amount)} | {payment_method}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"{prefix}{txn_id}")])

    keyboard += back

    if is_confirm:
        message = f"✅ Manual Payment Confirmation ({len(rows)} pending)\n\nSelect a transaction to confirm:"
    else:
        message = f"❌ Cancel Payments ({len(rows)} pending)\n\nSelect a transaction to cancel:"

    try:
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass


async def admin_confirm_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for manual confirmation."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()
    await _render_pending_txn_menu(query, "confirm")


async def admin_cancel_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of pending transactions for cancellation."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()
    await _render_pending_txn_menu(query, "cancel")


async def admin_confirm_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually confirm a pending payment transaction."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        txn_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    def _sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(id=txn_id).first()

            if not txn:
                return "not_found", None

            if txn.status != TransactionStatus.PENDING:
                return "already", txn.status.value

            user = session.query(User).filter_by(id=txn.user_id).first()
            if user:
                user.wallet_balance = to_money(user.wallet_balance + txn.amount)

            txn.status = TransactionStatus.COMPLETED
            txn.completed_at = datetime.utcnow()
            log_admin_action(session, update.effective_user.id, "confirm_payment",
                              target_type="transaction", target_id=txn.id,
                              details=f"amount={txn.amount}")
            session.commit()

            amount = txn.amount
            notify = (user.telegram_id, amount, user.wallet_balance) if user else None
            return "ok", (amount, notify)

    status, payload = await asyncio.to_thread(_sync)

    if status == "not_found":
        await query.answer("❌ Transaction not found.", show_alert=True)
        return
    if status == "already":
        await query.answer(f"⚠️ Transaction is already {payload}", show_alert=True)
        return

    amount, notify = payload
    await query.answer(f"✅ Payment confirmed! {format_price(amount)} added to user's wallet.", show_alert=True)

    if notify:
        telegram_id, amount, new_balance = notify
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"✅ Payment Confirmed!\n\n💰 Amount: {format_price(amount)}\n💵 New Balance: {format_price(new_balance)}"
            )
        except Exception:
            pass

    await _render_pending_txn_menu(query, "confirm")


async def admin_cancel_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending payment transaction."""
    query = update.callback_query

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    try:
        txn_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    def _sync():
        with get_db_session() as session:
            txn = session.query(Transaction).filter_by(id=txn_id).first()

            if not txn:
                return "not_found", None

            if txn.status != TransactionStatus.PENDING:
                return "already", txn.status.value

            txn.status = TransactionStatus.FAILED
            log_admin_action(session, update.effective_user.id, "cancel_payment",
                              target_type="transaction", target_id=txn.id,
                              details=f"amount={txn.amount}")
            session.commit()

            user = session.query(User).filter_by(id=txn.user_id).first()
            notify = (user.telegram_id, txn.amount) if user else None
            return "ok", notify

    status, payload = await asyncio.to_thread(_sync)

    if status == "not_found":
        await query.answer("❌ Transaction not found.", show_alert=True)
        return
    if status == "already":
        await query.answer(f"⚠️ Transaction is already {payload}", show_alert=True)
        return

    await query.answer("✅ Payment cancelled!", show_alert=True)

    if payload:
        telegram_id, amount = payload
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"❌ Payment Cancelled\n\n💰 Amount: {format_price(amount)}\n\nYour payment was not confirmed. Please contact support if you believe this is an error."
            )
        except Exception:
            pass

    await _render_pending_txn_menu(query, "cancel")


async def admin_cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel an order and refund the user."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    await query.answer()

    try:
        order_id = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            order = session.query(Order).filter_by(id=order_id).first()

            if not order:
                return "not_found", None

            # Guard: without this, every click on the button refunded the order
            # amount again.
            if order.status == OrderStatus.CANCELLED:
                return "already", None

            # Refund user
            user = session.query(User).filter_by(id=order.user_id).first()
            if user:
                user.wallet_balance = to_money(user.wallet_balance + order.total_amount)

            # Return the keys that were assigned to this order back to stock.
            returned_keys = session.query(ProductKey).filter_by(order_id=order.id, is_sold=True).all()
            for key in returned_keys:
                key.is_sold = False
                key.order_id = None
                key.sold_at = None
                product = session.query(Product).filter_by(id=key.product_id).first()
                if product:
                    product.stock_count += 1

            # Mark order as cancelled
            order.status = OrderStatus.CANCELLED
            log_admin_action(session, update.effective_user.id, "cancel_order",
                              target_type="order", target_id=order.id,
                              details=f"refund={order.total_amount}, keys_returned={len(returned_keys)}")
            session.commit()

            notify = (user.telegram_id, order.id, order.total_amount) if user else None
            return "ok", notify

    status, notify = await asyncio.to_thread(_sync)

    if status == "not_found":
        await query.edit_message_text("❌ Order not found.")
        return
    if status == "already":
        await query.edit_message_text("ℹ️ Order is already cancelled — no refund issued.")
        return

    if notify:
        telegram_id, oid, amount = notify
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"❌ Order #{oid} has been cancelled by admin.\n💰 Refund: {format_price(amount)}"
            )
        except Exception:
            pass

    # Refresh order details (query.answer() already fired above)
    await _render_order_detail(query, order_id)


async def cancel_restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the restock keys conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "❌ Restock cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Restock cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )

    # Clear restock_product_id from context
    context.user_data.pop('restock_product_id', None)

    return ConversationHandler.END
