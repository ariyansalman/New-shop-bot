"""User-facing command and callback handlers.

Every handler below that used to do "with get_db_session() as session: ...
query... await telegram_call(...)" now splits that in two: a nested _sync()
closure does the DB work (and any pure-Python formatting/keyboard-building
that needs the still-attached ORM objects) and runs off the event loop via
asyncio.to_thread, then the async function does only the actual Telegram
API await with the plain data _sync() returned. This matters because
python-telegram-bot runs everything on one event loop - a synchronous DB
query blocks every other user's handler until it returns, which is most
noticeable against a remote database (e.g. Supabase) where each query is a
network round trip rather than a local file read.
"""

import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_db_session, User, Category, Subcategory, Product, Order, OrderItem, Settings, ProductType, OrderStatus, DisputeStatus
from utils import (
    format_price, format_datetime, create_main_menu_keyboard,
    create_pagination_keyboard, create_product_detail_keyboard,
    create_support_keyboard, check_user_banned_async,
    paginate_items, format_product_display, build_availability_text,
    create_back_support_keyboard, t, SUPPORTED_LANGS
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show welcome message with wallet balance."""
    user = update.effective_user
    telegram_id = user.id
    username = user.username

    # Check if user is banned
    if await check_user_banned_async(telegram_id):
        await update.message.reply_text("⛔ You have been banned from using this bot.")
        return

    def _sync():
        # Get or create user and fetch settings in same session
        with get_db_session() as session:
            # Get or create user
            db_user = session.query(User).filter_by(telegram_id=telegram_id).first()
            if not db_user:
                db_user = User(telegram_id=telegram_id, username=username)
                session.add(db_user)
                session.commit()
                session.refresh(db_user)
            elif username and db_user.username != username:
                # The username was captured once at signup and then never refreshed,
                # so admin views showed stale handles.
                db_user.username = username
                session.commit()

            wallet_balance = db_user.wallet_balance
            lang = db_user.language

            # Get store settings
            store_settings = session.query(Settings).first()
            welcome_msg = store_settings.welcome_message if store_settings else "Welcome to our Digital Products Store!"
            logo_path = store_settings.store_logo_path if store_settings else None
            return wallet_balance, lang, welcome_msg, logo_path

    wallet_balance, lang, welcome_msg, logo_path = await asyncio.to_thread(_sync)

    # Send logo if available
    if logo_path and os.path.exists(logo_path):
        with open(logo_path, 'rb') as logo:
            await update.message.reply_photo(photo=logo)

    # Send welcome message with wallet balance
    balance_line = t('main_menu.wallet_balance', lang, balance=format_price(wallet_balance))
    message = f"{welcome_msg}\n\n{balance_line}"

    await update.message.reply_text(
        message,
        reply_markup=create_main_menu_keyboard(lang)
    )


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu callback - return to main menu."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is banned
    if await check_user_banned_async(user_id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    def _sync():
        with get_db_session() as session:
            # Get user
            db_user = session.query(User).filter_by(telegram_id=user_id).first()
            if not db_user:
                db_user = User(telegram_id=user_id)
                session.add(db_user)
                session.commit()
                session.refresh(db_user)

            wallet_balance = db_user.wallet_balance
            lang = db_user.language

            # Get store settings
            store_settings = session.query(Settings).first()
            welcome_msg = store_settings.welcome_message if store_settings else "Welcome to our Digital Products Store!"
            return wallet_balance, lang, welcome_msg

    wallet_balance, lang, welcome_msg = await asyncio.to_thread(_sync)

    balance_line = t('main_menu.wallet_balance', lang, balance=format_price(wallet_balance))
    message = f"{welcome_msg}\n\n{balance_line}"

    await query.edit_message_text(
        message,
        reply_markup=create_main_menu_keyboard(lang)
    )


async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the main-menu language toggle button (callback_data: set_lang_<code>)."""
    query = update.callback_query
    user_id = update.effective_user.id

    try:
        lang = query.data.split("_", 2)[2]
    except IndexError:
        lang = None
    if lang not in SUPPORTED_LANGS:
        await query.answer("❌ Invalid request.", show_alert=True)
        return

    def _sync():
        with get_db_session() as session:
            db_user = session.query(User).filter_by(telegram_id=user_id).first()
            if not db_user:
                return None
            db_user.language = lang

            wallet_balance = db_user.wallet_balance
            store_settings = session.query(Settings).first()
            welcome_msg = store_settings.welcome_message if store_settings else "Welcome to our Digital Products Store!"
            return wallet_balance, welcome_msg

    result = await asyncio.to_thread(_sync)
    if result is None:
        await query.answer("❌ User not found.", show_alert=True)
        return
    wallet_balance, welcome_msg = result

    await query.answer(t('language.saved', lang), show_alert=False)

    balance_line = t('main_menu.wallet_balance', lang, balance=format_price(wallet_balance))
    message = f"{welcome_msg}\n\n{balance_line}"
    await query.edit_message_text(message, reply_markup=create_main_menu_keyboard(lang))


async def products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle products button - show category list."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    # If coming from a photo message, delete it and create new text message
    if query.message.photo:
        await query.message.delete()
        message = await query.message.reply_text("Loading...")

        # Create mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        query = MockQuery(message)

    # Extract page number from callback data
    callback_data = query.data if hasattr(query, 'data') else "products"
    page = 0
    if "_page_" in callback_data:
        try:
            page = max(0, int(callback_data.split("_page_")[1]))
        except ValueError:
            page = 0

    def _sync():
        with get_db_session() as session:
            return [(cat.id, cat.name) for cat in session.query(Category).all()]

    categories = await asyncio.to_thread(_sync)

    if not categories:
        await query.edit_message_text(
            "📦 No categories available yet.",
            reply_markup=create_back_support_keyboard()
        )
        return

    # Paginate categories
    page_info = paginate_items(categories, page, page_size=5)

    # Create category buttons
    category_buttons = [
        [InlineKeyboardButton(name, callback_data=f"category_{cat_id}")]
        for cat_id, name in page_info['items']
    ]

    keyboard = create_pagination_keyboard(
        category_buttons,
        page_info['page'],
        page_info['total_pages'],
        "products"
    )

    text = "📦 Select a Category for the product you need:"
    if page_info['total_pages'] > 1:
        text += f"\n\nPage {page_info['page'] + 1} of {page_info['total_pages']}"

    await query.edit_message_text(text, reply_markup=keyboard)


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product callback."""
    await product_detail_callback(update, context)


async def subcategory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subcategory selection."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    try:
        subcategory_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    # If coming from a photo message (product detail with image), delete and send new message
    if query.message.photo:
        await query.message.delete()
        # Create a new text message for products list
        message = await query.message.reply_text("Loading products...")
        # Now we need to pass this message to show_products_list
        # We'll use a workaround by creating a mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        mock_query = MockQuery(message)
        await show_products_list(mock_query, subcategory_id=subcategory_id, context=context)
    else:
        await show_products_list(query, subcategory_id=subcategory_id, context=context)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection - show subcategories or products."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    callback_data = query.data
    try:
        category_id = int(callback_data.split("_")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    # If coming from a photo message, delete it and create new text message
    if query.message.photo:
        await query.message.delete()
        message = await query.message.reply_text("Loading...")

        # Create mock query object
        class MockQuery:
            def __init__(self, message):
                self.message = message
            async def edit_message_text(self, text, reply_markup=None):
                await self.message.edit_text(text, reply_markup=reply_markup)

        query = MockQuery(message)

    def _sync():
        with get_db_session() as session:
            category = session.query(Category).filter_by(id=category_id).first()
            if not category:
                return None

            # Check if category has subcategories
            subcategories = [
                (sub.id, sub.name)
                for sub in session.query(Subcategory).filter_by(category_id=category_id).all()
            ]
            return category.name, subcategories

    result = await asyncio.to_thread(_sync)
    if result is None:
        await query.edit_message_text("❌ Category not found.")
        return
    category_name, subcategories = result

    if subcategories:
        # Show subcategories
        # Previously hard-limited to [:5] with no pagination, so any
        # further subcategories were unreachable.
        subcat_buttons = [
            [InlineKeyboardButton(name, callback_data=f"subcategory_{sub_id}")]
            for sub_id, name in subcategories
        ]

        # Create keyboard with back to products
        keyboard = subcat_buttons + [[
            InlineKeyboardButton("🔙 Back", callback_data="back_to_products"),
            InlineKeyboardButton("☎️ Support", callback_data="support")
        ]]

        await query.edit_message_text(
            f"📦 Select the product you need from {category_name}:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Show products directly
        await show_products_list(query, category_id=category_id, context=context)


async def show_products_list(query, category_id=None, subcategory_id=None, page=0, context=None):
    """Show list of products for a category or subcategory."""
    def _sync():
        with get_db_session() as session:
            query_filter = Product.is_active.is_(True)

            if category_id:
                products_query = session.query(Product).filter(
                    Product.category_id == category_id,
                    Product.subcategory_id.is_(None),
                    query_filter
                )
            elif subcategory_id:
                products_query = session.query(Product).filter(
                    Product.subcategory_id == subcategory_id,
                    query_filter
                )
            else:
                products_query = session.query(Product).filter(query_filter)

            products = [
                (prod.id, prod.name, prod.price, prod.stock_count)
                for prod in products_query.all()
            ]

            # Determine back button based on what we're showing
            parent_category_id = None
            if subcategory_id:
                subcategory = session.query(Subcategory).filter_by(id=subcategory_id).first()
                if subcategory:
                    parent_category_id = subcategory.category_id

            return products, parent_category_id

    products, parent_category_id = await asyncio.to_thread(_sync)

    if not products:
        await query.edit_message_text(
            "📦 No products available in this category.",
            reply_markup=create_back_support_keyboard()
        )
        return

    # Paginate products
    page_info = paginate_items(products, page, page_size=5)

    # Create product buttons
    product_buttons = [
        [InlineKeyboardButton(
            f"{name} | {format_price(price)} | Available: {stock}",
            callback_data=f"product_{prod_id}"
        )]
        for prod_id, name, price, stock in page_info['items']
    ]

    # Add pagination if needed
    keyboard = product_buttons.copy()
    if page_info['total_pages'] > 1:
        # Scope: "plist_<cat|sub|all>_<id>_<page>"
        if subcategory_id:
            scope = f"sub_{subcategory_id}"
        elif category_id:
            scope = f"cat_{category_id}"
        else:
            scope = "all_0"

        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"plist_{scope}_{page-1}"))
        if page < page_info['total_pages'] - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"plist_{scope}_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    # Determine back button based on what we're showing
    if subcategory_id and parent_category_id:
        # Back to category (which will show subcategories)
        back_data = f"category_{parent_category_id}"
    else:
        back_data = "back_to_products"

    keyboard.append([
        InlineKeyboardButton("🔙 Back", callback_data=back_data),
        InlineKeyboardButton("☎️ Support", callback_data="support")
    ])

    text = "📦 Select the product you need:"
    if page_info['total_pages'] > 1:
        text += f"\n\nPage {page_info['page'] + 1} of {page_info['total_pages']}"

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def products_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle paging within a product list (callback: plist_<scope>_<id>_<page>)."""
    query = update.callback_query
    await query.answer()

    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    try:
        _, scope, scope_id, page = query.data.split("_")
        scope_id = int(scope_id)
        page = int(page)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    if page < 0:
        page = 0

    if scope == "cat":
        await show_products_list(query, category_id=scope_id, page=page, context=context)
    elif scope == "sub":
        await show_products_list(query, subcategory_id=scope_id, page=page, context=context)
    else:
        await show_products_list(query, page=page, context=context)


async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection - show product details."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    try:
        product_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    def _sync():
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=product_id).first()

            if not product:
                return None

            # Determine back navigation based on product's category/subcategory
            if product.subcategory_id:
                # Product belongs to a subcategory - go back to subcategory list
                back_callback = f"subcategory_{product.subcategory_id}"
            elif product.category_id:
                # Product belongs to a category - go back to category
                back_callback = f"category_{product.category_id}"
            else:
                # Fallback to products list
                back_callback = "back_to_products"

            # Format product details
            details = format_product_display(product, include_description=True)

            return details, product.image_path, back_callback

    result = await asyncio.to_thread(_sync)
    if result is None:
        await query.edit_message_text("❌ Product not found.")
        return
    details, image_path, back_callback = result

    # Send product image if available
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as image:
            await query.message.reply_photo(
                photo=image,
                caption=details,
                reply_markup=create_product_detail_keyboard(product_id, back_callback)
            )
        await query.message.delete()
    else:
        await query.edit_message_text(
            details,
            reply_markup=create_product_detail_keyboard(product_id, back_callback)
        )


async def availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle availability button - show all available products."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    def _sync():
        with get_db_session() as session:
            categories = session.query(Category).all()
            products_by_category = {}

            for category in categories:
                products = session.query(Product).filter_by(
                    category_id=category.id,
                    is_active=True
                ).limit(15).all()

                if products:
                    products_by_category[category.name] = products

            if not products_by_category:
                return None

            return build_availability_text(products_by_category)

    text = await asyncio.to_thread(_sync)

    if text is None:
        await query.edit_message_text(
            "📦 No products available yet.",
            reply_markup=create_back_support_keyboard()
        )
        return

    await query.edit_message_text(
        text,
        reply_markup=create_back_support_keyboard()
    )


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support button - show support page."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    def _sync():
        with get_db_session() as session:
            store_settings = session.query(Settings).first()
            support_username = store_settings.support_username if store_settings else ""
            channel_username = store_settings.channel_username if store_settings else ""
            return support_username, channel_username

    support_username, channel_username = await asyncio.to_thread(_sync)

    message = "☎️ My Shop is Open 24/7"

    await query.edit_message_text(
        message,
        reply_markup=create_support_keyboard(support_username, channel_username)
    )


async def order_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order history button - show user's order history as clickable list."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    user_id = update.effective_user.id

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()

            if not user:
                return None

            orders = session.query(Order).filter_by(user_id=user.id).order_by(Order.created_at.desc()).limit(10).all()
            return [(o.id, o.status, o.total_amount, o.dispute_status) for o in orders]

    orders = await asyncio.to_thread(_sync)

    if orders is None:
        await query.edit_message_text("❌ User not found.")
        return

    if not orders:
        await query.edit_message_text(
            "🛍 No orders yet.",
            reply_markup=create_back_support_keyboard()
        )
        return

    # Build keyboard with order buttons
    keyboard = []
    for order_id, status, total_amount, dispute_status in orders:
        status_emoji = {
            OrderStatus.PROCESSING: "⏳",
            OrderStatus.COMPLETED: "✅",
            OrderStatus.CANCELLED: "❌"
        }.get(status, "❓")

        dispute_indicator = ""
        if dispute_status == DisputeStatus.OPENED:
            dispute_indicator = " 🚨"
        elif dispute_status == DisputeStatus.RESOLVED:
            dispute_indicator = " ✔️"

        button_text = f"{status_emoji} Order #{order_id} | {format_price(total_amount)}{dispute_indicator}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"user_order_detail_{order_id}")])

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛍 Your Order History\n\nClick on an order to view details:",
        reply_markup=reply_markup
    )


async def user_order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order detail view with dispute button for user."""
    query = update.callback_query
    await query.answer()

    # Check if user is banned
    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return

    # Extract order_id from callback data
    try:
        order_id = int(query.data.split("_")[3])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return
    user_id = update.effective_user.id

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                return "no_user", None

            order = session.query(Order).filter_by(id=order_id, user_id=user.id).first()
            if not order:
                return "no_order", None

            order_items = session.query(OrderItem).filter_by(order_id=order.id).all()

            # Build order details message
            items_text = ""
            for item in order_items:
                product = item.product  # may be None if the product was deleted
                product_name = product.name if product else "Deleted product"
                items_text += f"  📦 {product_name} (x{item.quantity}) - {format_price(item.price * item.quantity)}\n"

                # Add delivered assets (keys or download links)
                if item.delivered_asset:
                    if product and product.product_type == ProductType.KEY:
                        items_text += f"  🔐 Keys:\n{item.delivered_asset}\n"
                    elif product and product.product_type == ProductType.FILE:
                        items_text += f"  🔗 Download: {item.delivered_asset}\n"
                    else:
                        items_text += f"  📄 {item.delivered_asset}\n"
                items_text += "\n"

            status_emoji = {
                OrderStatus.PROCESSING: "⏳",
                OrderStatus.COMPLETED: "✅",
                OrderStatus.CANCELLED: "❌"
            }.get(order.status, "❓")

            dispute_text = ""
            if order.dispute_status == DisputeStatus.OPENED:
                dispute_text = "\n🚨 Dispute Status: OPEN - Admin will review soon"
            elif order.dispute_status == DisputeStatus.RESOLVED:
                dispute_text = "\n✔️ Dispute Status: RESOLVED"

            message = f"""🛍 Order Details

🔸 Order #{order.id}
{status_emoji} Status: {order.status.value}
💰 Total Amount: {format_price(order.total_amount)}
📅 Date: {format_datetime(order.created_at)}

📦 Items:
{items_text}{dispute_text}"""

            # Build keyboard based on order status
            keyboard = []

            # Add dispute button if no dispute is open/resolved
            if order.dispute_status == DisputeStatus.NIL:
                keyboard.append([InlineKeyboardButton("🚨 Open Dispute", callback_data=f"open_dispute_{order.id}")])

            # Add back button
            keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="order_history")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            return "ok", (message, reply_markup)

    status, payload = await asyncio.to_thread(_sync)

    if status == "no_user":
        await query.edit_message_text("❌ User not found.")
        return
    if status == "no_order":
        await query.edit_message_text("❌ Order not found.")
        return

    message, reply_markup = payload
    await query.edit_message_text(message, reply_markup=reply_markup)


async def back_to_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back to products - show category list."""
    # Just redirect to products_callback
    await products_callback(update, context)
