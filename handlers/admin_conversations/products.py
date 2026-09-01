"""Product creation and edit conversation flows.

Same asyncio.to_thread refactor as the other handler modules: every
"with get_db_session()" block's query/mutation work runs in a nested
_sync() closure off the event loop, returning only plain data, with the
final Telegram API call left on the event loop. This is a multi-step
admin-only conversation (product creation, product editing) rather than a
hot path any regular user hits, but a blocking query here still freezes
every other user's handler for as long as it runs.
"""

import asyncio
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, Category, Subcategory, Product, ProductType
)
from utils import is_admin, format_price, money_or_none, log_admin_action, create_admin_product_menu_keyboard
from config.settings import settings as app_settings

logger = logging.getLogger(__name__)

# Conversation states
# Conversation states for product creation
PRODUCT_NAME, PRODUCT_DESC, PRODUCT_PRICE, PRODUCT_TYPE, PRODUCT_CATEGORY, PRODUCT_SUBCATEGORY, PRODUCT_IMAGE, PRODUCT_DOWNLOAD_LINK, PRODUCT_KEYS = range(9)

# Conversation states for product edit
EDIT_SELECT_PRODUCT, EDIT_SELECT_FIELD, EDIT_NEW_VALUE, EDIT_IMAGE_VALUE = range(4)


# ==================== PRODUCT CREATION FLOW ====================

async def create_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start product creation flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    # Create cancel button
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    await query.edit_message_text(
        "📦 Create New Product\n\nPlease enter the product name:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PRODUCT_NAME


async def product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product name input."""
    context.user_data['product_name'] = update.message.text

    # Create cancel button
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    await update.message.reply_text(
        "📝 Please enter the product description:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PRODUCT_DESC


async def product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product description input."""
    context.user_data['product_desc'] = update.message.text

    # Create cancel button
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    await update.message.reply_text(
        "💰 Please enter the product price (USD):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PRODUCT_PRICE


async def product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product price input."""
    # Create cancel button for error messages
    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    price = money_or_none(update.message.text)
    if price is None:
        await update.message.reply_text(
            "❌ Invalid price. Please enter a number:",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_PRICE

    if price <= 0:
        await update.message.reply_text(
            "❌ Price must be greater than 0. Please enter a valid price:",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_PRICE

    context.user_data['product_price'] = price

    # Ask for product type
    keyboard = [
        [InlineKeyboardButton("🔑 Software Key", callback_data="type_key")],
        [InlineKeyboardButton("📁 Downloadable File", callback_data="type_file")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]
    ]
    await update.message.reply_text(
        "📦 Select product type:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PRODUCT_TYPE


async def product_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product type selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_product":
        await query.edit_message_text(
            "❌ Product creation cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    product_type = ProductType.KEY if query.data == "type_key" else ProductType.FILE
    context.user_data['product_type'] = product_type

    def _sync():
        with get_db_session() as session:
            categories = session.query(Category).all()
            return [(c.id, c.name) for c in categories]

    categories = await asyncio.to_thread(_sync)

    if not categories:
        await query.edit_message_text(
            "❌ No categories available. Please create a category first.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    keyboard = []
    for cat_id, name in categories:
        keyboard.append([InlineKeyboardButton(name, callback_data=f"cat_{cat_id}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")])

    await query.edit_message_text(
        "📁 Select a category for this product:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PRODUCT_CATEGORY


async def product_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product category selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_product":
        await query.edit_message_text(
            "❌ Product creation cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    category_id = int(query.data.split("_")[1])
    context.user_data['product_category'] = category_id

    def _sync():
        with get_db_session() as session:
            subcategories = session.query(Subcategory).filter_by(category_id=category_id).all()
            return [(s.id, s.name) for s in subcategories]

    subcategories = await asyncio.to_thread(_sync)

    if subcategories:
        keyboard = [[InlineKeyboardButton("⏭ Skip (No Subcategory)", callback_data="subcat_skip")]]
        for sub_id, name in subcategories:
            keyboard.append([InlineKeyboardButton(name, callback_data=f"subcat_{sub_id}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")])

        await query.edit_message_text(
            "📂 Select a subcategory (optional):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PRODUCT_SUBCATEGORY
    else:
        # No subcategories, skip to image
        context.user_data['product_subcategory'] = None

        # Create cancel button
        cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

        await query.edit_message_text(
            "🖼 Send a product image (optional) or type 'skip' to skip:",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_IMAGE


async def product_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product subcategory selection."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_product":
        await query.edit_message_text(
            "❌ Product creation cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "subcat_skip":
        context.user_data['product_subcategory'] = None
    else:
        subcategory_id = int(query.data.split("_")[1])
        context.user_data['product_subcategory'] = subcategory_id

    # Create cancel button
    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    await query.edit_message_text(
        "🖼 Send a product image (optional) or type 'skip' to skip:",
        reply_markup=InlineKeyboardMarkup(cancel_keyboard)
    )
    return PRODUCT_IMAGE


async def product_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product image upload."""
    # Create cancel button
    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    if update.message.text and update.message.text.lower() == 'skip':
        context.user_data['product_image'] = None

        # Check if it's a file type product
        if context.user_data['product_type'] == ProductType.FILE:
            await update.message.reply_text(
                "🔗 Please enter the download link for this file product:",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_DOWNLOAD_LINK
        else:
            # Ask for keys for KEY products
            await update.message.reply_text(
                "🔑 Please paste the product keys (one per line) or upload a .txt file:\n\n"
                "Example:\n"
                "KEY1-XXXX-XXXX-XXXX\n"
                "KEY2-XXXX-XXXX-XXXX\n"
                "KEY3-XXXX-XXXX-XXXX\n\n"
                "Or type 'skip' to add keys later.",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_KEYS

    elif update.message.photo:
        # Download and save image
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_path = os.path.join(
            app_settings.PRODUCTS_DIR,
            f"product_{int(datetime.utcnow().timestamp())}.jpg"
        )
        os.makedirs(app_settings.PRODUCTS_DIR, exist_ok=True)
        await file.download_to_drive(image_path)

        context.user_data['product_image'] = image_path

        # Check if it's a file type product
        if context.user_data['product_type'] == ProductType.FILE:
            await update.message.reply_text(
                "🔗 Please enter the download link for this file product:",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_DOWNLOAD_LINK
        else:
            # Ask for keys for KEY products
            await update.message.reply_text(
                "🔑 Please paste the product keys (one per line) or upload a .txt file:\n\n"
                "Example:\n"
                "KEY1-XXXX-XXXX-XXXX\n"
                "KEY2-XXXX-XXXX-XXXX\n"
                "KEY3-XXXX-XXXX-XXXX\n\n"
                "Or type 'skip' to add keys later.",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_KEYS
    else:
        await update.message.reply_text(
            "❌ Please send an image or type 'skip':",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_IMAGE


async def product_download_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle download link for file products."""
    link = (update.message.text or "").strip()

    if not link.startswith(("http://", "https://")):
        cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]
        await update.message.reply_text(
            "❌ Please enter a valid link starting with http:// or https://",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_DOWNLOAD_LINK

    context.user_data['product_download_link'] = link

    # Create product
    await create_product_final(update, context)
    return ConversationHandler.END


async def product_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product keys input for KEY products (text or file upload)."""
    from utils import parse_keys_from_text

    # Create cancel button for error messages
    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_product")]]

    # Handle file upload
    if update.message.document:
        try:
            # Download file
            document = update.message.document
            if document.file_size and document.file_size > 1024 * 1024:
                await update.message.reply_text(
                    "❌ File is too large (max 1024 KB). Try again or type 'skip':",
                    reply_markup=InlineKeyboardMarkup(cancel_keyboard)
                )
                return PRODUCT_KEYS

            file = await context.bot.get_file(document.file_id)
            file_content = await file.download_as_bytearray()

            # Parse keys from file (errors='replace' so a non-UTF-8 file cannot crash us)
            text = bytes(file_content).decode('utf-8', errors='replace')
            keys = parse_keys_from_text(text)

            if not keys:
                await update.message.reply_text(
                    "❌ No valid keys found in file. Try again, paste keys directly, or type 'skip':",
                    reply_markup=InlineKeyboardMarkup(cancel_keyboard)
                )
                return PRODUCT_KEYS

            # Store keys temporarily
            context.user_data['product_keys'] = keys

            # Create product with keys
            await create_product_final(update, context)
            return ConversationHandler.END

        except Exception as e:
            await update.message.reply_text(
                f"❌ Error reading file: {str(e)}\n\nTry again, paste keys directly, or type 'skip':",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_KEYS

    # Handle text input
    elif update.message.text:
        if update.message.text.lower() == 'skip':
            # Skip adding keys for now
            context.user_data['product_keys'] = []
            await create_product_final(update, context)
            return ConversationHandler.END

        # Parse keys from pasted text
        keys = parse_keys_from_text(update.message.text)

        if not keys:
            await update.message.reply_text(
                "❌ No valid keys found. Please paste keys (one per line), upload a .txt file, or type 'skip':",
                reply_markup=InlineKeyboardMarkup(cancel_keyboard)
            )
            return PRODUCT_KEYS

        # Store keys temporarily
        context.user_data['product_keys'] = keys

        # Create product with keys
        await create_product_final(update, context)
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            "❌ Please paste keys, upload a .txt file, or type 'skip':",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return PRODUCT_KEYS


async def create_product_final(update, context):
    """Create the product in the database."""
    from database import ProductKey

    # Pull everything needed out of context.user_data up front, so the
    # thread closure below only ever touches plain local values, never
    # context.user_data itself.
    raw_keys = context.user_data.get('product_keys', []) or []
    product_type_value = context.user_data['product_type']
    new_product_name = context.user_data['product_name']
    product_desc_value = context.user_data['product_desc']
    product_price_value = context.user_data['product_price']
    product_category_id = context.user_data['product_category']
    product_subcategory_id = context.user_data.get('product_subcategory')
    product_image_path = context.user_data.get('product_image')
    product_download_link_value = context.user_data.get('product_download_link')

    def _sync():
        with get_db_session() as session:
            # Get keys if provided, de-duplicated (a pasted list often repeats keys,
            # which previously inflated stock_count and could deliver the same key twice)
            seen = set()
            product_keys = []
            for k in raw_keys:
                if k not in seen:
                    seen.add(k)
                    product_keys.append(k)
            duplicates_dropped = len(raw_keys) - len(product_keys)
            stock_count = len(product_keys)

            # For file products, set stock to 999999 (unlimited)
            if product_type_value == ProductType.FILE:
                stock_count = 999999

            product = Product(
                name=new_product_name,
                description=product_desc_value,
                price=product_price_value,
                product_type=product_type_value,
                category_id=product_category_id,
                subcategory_id=product_subcategory_id,
                image_path=product_image_path,
                download_link=product_download_link_value,
                stock_count=stock_count,
                is_active=True
            )
            session.add(product)
            session.commit()
            session.refresh(product)

            # Add keys to product_keys table if provided
            keys_added = 0
            if product_keys and product_type_value == ProductType.KEY:
                for key_value in product_keys:
                    product_key = ProductKey(
                        product_id=product.id,
                        key_value=key_value,
                        is_sold=False
                    )
                    session.add(product_key)
                    keys_added += 1
                session.commit()

            # Build success message
            message = f"""✅ Product Created Successfully!

📦 Name: {product.name}
💰 Price: {format_price(product.price)}
📝 Type: {product.product_type.value}
📁 Category ID: {product.category_id}

Product ID: #{product.id}"""

            if keys_added > 0:
                message += f"\n🔑 Keys Added: {keys_added}"
                if duplicates_dropped:
                    message += f"\n⏭ Duplicates dropped: {duplicates_dropped}"
            elif product_type_value == ProductType.KEY:
                message += "\n\n⚠️ No keys added. Use the Restock Keys option to add inventory."

            return message

    message = await asyncio.to_thread(_sync)

    # Create keyboard with options
    keyboard = [
        [InlineKeyboardButton("➕ Create Another Product", callback_data="admin_create_product")],
        [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, reply_markup=reply_markup)

    context.user_data.clear()


async def cancel_product_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel product creation."""
    from utils import create_admin_product_menu_keyboard

    # Handle both callback query and message
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Product creation cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Product creation cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END



# ==================== PRODUCT EDIT FLOW ====================

async def edit_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start product edit flow - show paginated product list."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    # Get page number from callback data (default to 0)
    page = 0
    if "_page_" in query.data:
        page = int(query.data.split("_page_")[1])

    def _sync():
        with get_db_session() as session:
            all_products = session.query(Product).order_by(Product.id).all()
            return [(p.id, p.name, p.price, p.is_active) for p in all_products]

    products_data = await asyncio.to_thread(_sync)

    if not products_data:
        await query.edit_message_text(
            "❌ No products found. Please create a product first.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        return ConversationHandler.END

    # Pagination settings
    items_per_page = 5
    total_pages = (len(products_data) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_products = products_data[start_idx:end_idx]

    # Build product selection keyboard
    keyboard = []
    for product_id, prod_name, price, is_active in page_products:
        status_icon = "✅" if is_active else "❌"
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {prod_name} (${price})",
                callback_data=f"edit_prod_{product_id}"
            )
        ])

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_edit_product_page_{page-1}"))
        pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_edit_product_page_{page+1}"))
        keyboard.append(pagination_row)

    # Add cancel button
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_products")])

    await query.edit_message_text(
        "✏️ Edit Product\n\nSelect a product to edit:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_SELECT_PRODUCT


async def edit_select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection from the list."""
    query = update.callback_query
    await query.answer()

    # Handle pagination - stay in EDIT_SELECT_PRODUCT state
    if "admin_edit_product_page_" in query.data:
        return await edit_product_start(update, context)

    # Extract product ID from callback data
    product_id = int(query.data.split("_")[2])

    def _sync():
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=product_id).first()

            if not product:
                return None

            # Get current category and subcategory names
            category_name = "None"
            subcategory_name = "None"
            if product.category_id:
                category = session.query(Category).filter_by(id=product.category_id).first()
                category_name = category.name if category else "None"
            if product.subcategory_id:
                subcategory = session.query(Subcategory).filter_by(id=product.subcategory_id).first()
                subcategory_name = subcategory.name if subcategory else "None"

            # Get available keys count
            from database import ProductKey
            available_keys = session.query(ProductKey).filter_by(product_id=product.id, is_sold=False).count()

            return product.name, product.price, category_name, subcategory_name, product.is_active, available_keys

    result = await asyncio.to_thread(_sync)

    if result is None:
        await query.edit_message_text(
            "❌ Product not found.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        return ConversationHandler.END

    context.user_data['edit_product_id'] = product_id
    prod_name, price, category_name, subcategory_name, is_active, available_keys = result

    # Show fields to edit
    keyboard = [
        [InlineKeyboardButton("📦 Name", callback_data="edit_name")],
        [InlineKeyboardButton("📝 Description", callback_data="edit_desc")],
        [InlineKeyboardButton("💰 Price", callback_data="edit_price")],
        [InlineKeyboardButton("🖼 Image", callback_data="edit_image")],
        [InlineKeyboardButton("📁 Category", callback_data="edit_category")],
        [InlineKeyboardButton("📂 Subcategory", callback_data="edit_subcategory")],
        [InlineKeyboardButton("✅ Activate", callback_data="edit_activate")],
        [InlineKeyboardButton("❌ Deactivate", callback_data="edit_deactivate")],
        [InlineKeyboardButton(f"🗑 Clear Keys ({available_keys})", callback_data="edit_clear_keys")],
        [InlineKeyboardButton("🗑 Delete Product", callback_data="edit_delete")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="cancel_edit")]
    ]

    current_status = "Active" if is_active else "Inactive"

    await query.edit_message_text(
        f"Editing Product: {prod_name}\n"
        f"Current Price: {format_price(price)}\n"
        f"Category: {category_name}\n"
        f"Subcategory: {subcategory_name}\n"
        f"Status: {current_status}\n"
        f"Available Keys: {available_keys}\n\n"
        f"What would you like to edit?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_SELECT_FIELD


async def edit_select_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection for editing."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_edit":
        await query.edit_message_text(
            "❌ Product edit cancelled.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    product_id = context.user_data['edit_product_id']

    if query.data == "edit_activate":
        def _sync():
            with get_db_session() as session:
                product = session.query(Product).filter_by(id=product_id).first()
                product.is_active = True
                session.commit()
                return product.name

        prod_name = await asyncio.to_thread(_sync)
        await query.edit_message_text(
            f"✅ Product '{prod_name}' activated!",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "edit_deactivate":
        def _sync():
            with get_db_session() as session:
                product = session.query(Product).filter_by(id=product_id).first()
                product.is_active = False
                session.commit()
                return product.name

        prod_name = await asyncio.to_thread(_sync)
        await query.edit_message_text(
            f"❌ Product '{prod_name}' deactivated!",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "edit_clear_keys":
        def _sync():
            with get_db_session() as session:
                from database import ProductKey
                product = session.query(Product).filter_by(id=product_id).first()
                prod_name = product.name

                # Count and delete only unsold keys
                unsold_keys_count = session.query(ProductKey).filter_by(
                    product_id=product.id, is_sold=False
                ).count()

                if unsold_keys_count == 0:
                    return prod_name, 0

                session.query(ProductKey).filter_by(
                    product_id=product.id, is_sold=False
                ).delete()
                # Update stock count
                product.stock_count = 0
                session.commit()
                return prod_name, unsold_keys_count

        prod_name, cleared_count = await asyncio.to_thread(_sync)

        if cleared_count == 0:
            await query.edit_message_text(
                f"ℹ️ Product '{prod_name}' has no unsold keys to clear.",
                reply_markup=create_admin_product_menu_keyboard()
            )
        else:
            await query.edit_message_text(
                f"✅ Cleared {cleared_count} unsold key(s) from '{prod_name}'!\n\n"
                f"You can now add new keys using the Restock option.",
                reply_markup=create_admin_product_menu_keyboard()
            )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "edit_delete":
        def _sync():
            with get_db_session() as session:
                from database import Cart, OrderItem, ProductKey
                product = session.query(Product).filter_by(id=product_id).first()
                prod_name = product.name

                # Delete associated cart items
                session.query(Cart).filter_by(product_id=product.id).delete()

                # Detach the order lines instead of letting them go with the
                # product: they are the customers' receipts. Each keeps its
                # quantity, the price actually paid and the delivered
                # keys/link in delivered_asset, and both order-detail views
                # already render a missing product as "Deleted product" /
                # "Unknown Product". Doing this explicitly (rather than
                # leaving it to SQLAlchemy's implicit FK nulling on delete)
                # keeps it a single UPDATE and makes the intent visible.
                session.query(OrderItem).filter_by(product_id=product.id).update(
                    {"product_id": None}, synchronize_session=False
                )

                # Drop the whole key inventory for this product, sold rows
                # included. Product.product_keys cascades delete-orphan, so
                # session.delete(product) below would remove them regardless -
                # and the keys the customer actually received are not lost
                # with them: confirm_purchase copies the key text into
                # OrderItem.delivered_asset at sale time, which is what the
                # order history renders from.
                session.query(ProductKey).filter_by(product_id=product.id).delete(
                    synchronize_session=False
                )

                session.expire(product, ['order_items', 'product_keys'])
                session.delete(product)
                session.commit()
                return prod_name

        prod_name = await asyncio.to_thread(_sync)
        await query.edit_message_text(
            f"✅ Product '{prod_name}' deleted successfully!\n\n"
            f"Note: past orders keep their items, prices and delivered keys - "
            f"they now show the product as \"Deleted product\".",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "edit_image":
        def _sync():
            with get_db_session() as session:
                product = session.query(Product).filter_by(id=product_id).first()
                current_image_status = "Has image" if (product.image_path and os.path.exists(product.image_path)) else "No image"
                return product.name, current_image_status

        prod_name, current_image_status = await asyncio.to_thread(_sync)

        keyboard = [
            [InlineKeyboardButton("🗑 Remove Image", callback_data="remove_product_image")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]
        ]

        await query.edit_message_text(
            f"🖼 Product: {prod_name}\n"
            f"Current: {current_image_status}\n\n"
            f"Send a new product image or use the buttons below:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_IMAGE_VALUE

    if query.data == "edit_category":
        def _sync():
            with get_db_session() as session:
                categories = session.query(Category).all()
                return [(c.id, c.name) for c in categories]

        categories = await asyncio.to_thread(_sync)

        if not categories:
            await query.edit_message_text(
                "❌ No categories available. Please create a category first.",
                reply_markup=create_admin_product_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        keyboard = []
        for cat_id, name in categories:
            keyboard.append([InlineKeyboardButton(name, callback_data=f"newprodcat_{cat_id}")])
        keyboard.append([InlineKeyboardButton("🗑 Remove Category", callback_data="newprodcat_none")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")])

        context.user_data['edit_field'] = 'category'

        await query.edit_message_text(
            "📁 Select new category for this product:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_NEW_VALUE

    if query.data == "edit_subcategory":
        def _sync():
            with get_db_session() as session:
                # Get product to know current category
                product = session.query(Product).filter_by(id=product_id).first()

                # Get subcategories (optionally filter by product's category)
                if product.category_id:
                    subcategories = session.query(Subcategory).filter_by(category_id=product.category_id).all()
                else:
                    subcategories = session.query(Subcategory).all()

                return [(s.id, s.name) for s in subcategories]

        subcategories = await asyncio.to_thread(_sync)

        keyboard = []
        for sub_id, name in subcategories:
            keyboard.append([InlineKeyboardButton(name, callback_data=f"newprodsubcat_{sub_id}")])
        keyboard.append([InlineKeyboardButton("🗑 Remove Subcategory", callback_data="newprodsubcat_none")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")])

        context.user_data['edit_field'] = 'subcategory'

        await query.edit_message_text(
            "📂 Select new subcategory for this product:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_NEW_VALUE

    context.user_data['edit_field'] = query.data.split("_")[1]
    field = context.user_data['edit_field']

    # Get current product data to show old value
    def _sync():
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=product_id).first()

            if not product:
                return "not_found"

            if field == 'name':
                return f"📦 Current name: {product.name}\n\nEnter new product name:"
            elif field == 'desc':
                return f"📝 Current description:\n{product.description or 'No description'}\n\nEnter new product description:"
            elif field == 'price':
                return f"💰 Current price: {format_price(product.price)}\n\nEnter new product price (USD):"
            else:
                return "unknown_field"

    prompt = await asyncio.to_thread(_sync)

    if prompt == "not_found":
        await query.edit_message_text(
            "❌ Product not found.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END
    if prompt == "unknown_field":
        await query.edit_message_text(
            "❌ Unknown field.",
            reply_markup=create_admin_product_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]]
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(cancel_keyboard)
    )
    return EDIT_NEW_VALUE


async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new value input for edited field."""
    field = context.user_data['edit_field']

    # Handle category and subcategory changes (callback queries)
    if field in ['category', 'subcategory']:
        query = update.callback_query
        await query.answer()

        if query.data == "cancel_edit":
            await query.edit_message_text(
                "❌ Product edit cancelled.",
                reply_markup=create_admin_product_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        product_id = context.user_data['edit_product_id']
        callback_data = query.data

        def _sync():
            with get_db_session() as session:
                product = session.query(Product).filter_by(id=product_id).first()

                if field == 'category':
                    if callback_data == "newprodcat_none":
                        product.category_id = None
                        product.subcategory_id = None  # Clear subcategory when removing category
                    else:
                        new_category_id = int(callback_data.split("_")[1])
                        product.category_id = new_category_id
                        # Clear subcategory if it doesn't belong to new category
                        if product.subcategory_id:
                            subcat = session.query(Subcategory).filter_by(id=product.subcategory_id).first()
                            if subcat and subcat.category_id != new_category_id:
                                product.subcategory_id = None

                elif field == 'subcategory':
                    if callback_data == "newprodsubcat_none":
                        product.subcategory_id = None
                    else:
                        new_subcategory_id = int(callback_data.split("_")[1])
                        product.subcategory_id = new_subcategory_id
                        # Update category to match subcategory's parent
                        subcat = session.query(Subcategory).filter_by(id=new_subcategory_id).first()
                        if subcat:
                            product.category_id = subcat.category_id

                session.commit()

        await asyncio.to_thread(_sync)

        keyboard = [
            [InlineKeyboardButton("✏️ Edit Another Product", callback_data="admin_edit_product")],
            [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ Product {field} updated successfully!",
            reply_markup=reply_markup
        )

        context.user_data.clear()
        return ConversationHandler.END

    # Handle text input (name, desc, price)
    new_value = update.message.text
    product_id = context.user_data['edit_product_id']
    admin_telegram_id = update.effective_user.id

    def _sync():
        with get_db_session() as session:
            product = session.query(Product).filter_by(id=product_id).first()

            if field == 'name':
                product.name = new_value
            elif field == 'desc':
                product.description = new_value
            elif field == 'price':
                new_price = money_or_none(new_value)
                if new_price is None or new_price <= 0:
                    return "invalid_price"
                log_admin_action(session, admin_telegram_id, "edit_product_price",
                                  target_type="product", target_id=product.id,
                                  details=f"old={product.price}, new={new_price}")
                product.price = new_price

            session.commit()
            return "ok"

    result = await asyncio.to_thread(_sync)

    if result == "invalid_price":
        cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]]
        await update.message.reply_text(
            "❌ Invalid price. Please enter a valid number greater than 0:",
            reply_markup=InlineKeyboardMarkup(cancel_keyboard)
        )
        return EDIT_NEW_VALUE

    keyboard = [
        [InlineKeyboardButton("✏️ Edit Another Product", callback_data="admin_edit_product")],
        [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"✅ Product {field} updated successfully!",
        reply_markup=reply_markup
    )

    context.user_data.clear()
    return ConversationHandler.END


async def edit_image_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product image edit - receives new image or remove command."""
    # Handle callback queries (remove image or cancel)
    if update.callback_query:
        query = update.callback_query
        await query.answer()

        if query.data == "cancel_edit":
            await query.edit_message_text(
                "❌ Product edit cancelled.",
                reply_markup=create_admin_product_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        if query.data == "remove_product_image":
            product_id = context.user_data['edit_product_id']

            def _sync():
                with get_db_session() as session:
                    product = session.query(Product).filter_by(id=product_id).first()

                    # Delete old image file if exists
                    if product.image_path and os.path.exists(product.image_path):
                        try:
                            os.remove(product.image_path)
                        except Exception:
                            logger.exception("Error deleting old image")

                    product.image_path = None
                    session.commit()

            await asyncio.to_thread(_sync)

            keyboard = [
                [InlineKeyboardButton("✏️ Edit Another Product", callback_data="admin_edit_product")],
                [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "✅ Product image removed successfully!",
                reply_markup=reply_markup
            )

            context.user_data.clear()
            return ConversationHandler.END

    # Handle photo upload
    if update.message and update.message.photo:
        photo = update.message.photo[-1]  # Get highest resolution
        file = await context.bot.get_file(photo.file_id)

        # Create products directory if not exists
        products_dir = app_settings.PRODUCTS_DIR
        os.makedirs(products_dir, exist_ok=True)

        # Save with unique filename
        product_id = context.user_data['edit_product_id']
        image_path = os.path.join(products_dir, f"product_{product_id}_{photo.file_id}.jpg")
        await file.download_to_drive(image_path)

        def _sync():
            with get_db_session() as session:
                product = session.query(Product).filter_by(id=product_id).first()

                # Delete old image file if exists
                if product.image_path and os.path.exists(product.image_path):
                    try:
                        os.remove(product.image_path)
                    except Exception:
                        logger.exception("Error deleting old image")

                product.image_path = image_path
                session.commit()

        await asyncio.to_thread(_sync)

        keyboard = [
            [InlineKeyboardButton("✏️ Edit Another Product", callback_data="admin_edit_product")],
            [InlineKeyboardButton("🔙 Back to Product Menu", callback_data="admin_products")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "✅ Product image updated successfully!",
            reply_markup=reply_markup
        )

        context.user_data.clear()
        return ConversationHandler.END

    # Invalid input
    await update.message.reply_text(
        "❌ Please send an image or use the buttons provided."
    )
    return EDIT_IMAGE_VALUE
