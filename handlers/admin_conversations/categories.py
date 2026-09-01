"""Category and subcategory creation and edit conversation flows."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, Category, Subcategory, Product
)
from utils import is_admin, create_admin_category_menu_keyboard

logger = logging.getLogger(__name__)

# Conversation states
# Conversation states for category management
CATEGORY_NAME, CATEGORY_DESC = range(2)

# Conversation states for subcategory management
SUBCATEGORY_CATEGORY, SUBCATEGORY_NAME = range(2)
# Conversation states for category edit
EDIT_CATEGORY_SELECT, EDIT_CATEGORY_FIELD, EDIT_CATEGORY_VALUE = range(3)

# Conversation states for subcategory edit
EDIT_SUBCATEGORY_SELECT, EDIT_SUBCATEGORY_FIELD, EDIT_SUBCATEGORY_VALUE = range(3)


# ==================== CATEGORY MANAGEMENT FLOW ====================

async def create_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start category creation flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    await query.edit_message_text("📁 Create New Category\n\nPlease enter the category name:")
    return CATEGORY_NAME


async def category_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category name input."""
    context.user_data['category_name'] = update.message.text
    await update.message.reply_text("📝 Please enter the category description (or type 'skip'):")
    return CATEGORY_DESC


async def category_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category description input."""
    desc = update.message.text if update.message.text.lower() != 'skip' else ""
    context.user_data['category_desc'] = desc

    # Create category
    with get_db_session() as session:
        category = Category(
            name=context.user_data['category_name'],
            description=context.user_data['category_desc']
        )
        session.add(category)
        session.commit()

        keyboard = [
            [InlineKeyboardButton("➕ Create Another Category", callback_data="admin_create_category")],
            [InlineKeyboardButton("🔙 Back to Category Menu", callback_data="admin_manage_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ Category '{category.name}' created successfully!\nCategory ID: #{category.id}",
            reply_markup=reply_markup
        )

    context.user_data.clear()
    return ConversationHandler.END


# ==================== SUBCATEGORY MANAGEMENT FLOW ====================

async def create_subcategory_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start subcategory creation flow."""
    query = update.callback_query

    # Authorize before answering: answering twice is rejected by Telegram,
    # so the "Access denied" alert never reached the user before.
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    # Show categories
    with get_db_session() as session:
        categories = session.query(Category).all()

        if not categories:
            await query.edit_message_text("❌ No categories available. Please create a category first.")
            return ConversationHandler.END

        keyboard = []
        for cat in categories:
            keyboard.append([InlineKeyboardButton(cat.name, callback_data=f"subcat_cat_{cat.id}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_subcat")])

        await query.edit_message_text(
            "📂 Create New Subcategory\n\nSelect parent category:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SUBCATEGORY_CATEGORY


async def subcategory_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection for subcategory."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_subcat":
        from utils import create_admin_category_menu_keyboard
        await query.edit_message_text(
            "❌ Subcategory creation cancelled.",
            reply_markup=create_admin_category_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    category_id = int(query.data.split("_")[-1])
    context.user_data['subcategory_category'] = category_id

    await query.edit_message_text("📝 Please enter the subcategory name:")
    return SUBCATEGORY_NAME


async def subcategory_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subcategory name input."""
    # Create subcategory
    with get_db_session() as session:
        subcategory = Subcategory(
            name=update.message.text,
            category_id=context.user_data['subcategory_category']
        )
        session.add(subcategory)
        session.commit()

        category = session.query(Category).filter_by(id=subcategory.category_id).first()

        keyboard = [
            [InlineKeyboardButton("➕ Create Another Subcategory", callback_data="admin_create_subcategory")],
            [InlineKeyboardButton("🔙 Back to Category Menu", callback_data="admin_manage_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ Subcategory '{subcategory.name}' created under '{category.name}'!\n"
            f"Subcategory ID: #{subcategory.id}",
            reply_markup=reply_markup
        )

    context.user_data.clear()
    return ConversationHandler.END


# ==================== CATEGORY EDIT FLOW ====================

async def edit_category_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start category edit flow - show paginated category list."""
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

    with get_db_session() as session:
        # Get all categories
        all_categories = session.query(Category).order_by(Category.id).all()

        if not all_categories:
            await query.edit_message_text(
                "❌ No categories found. Please create a category first.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            return ConversationHandler.END

        # Pagination settings
        items_per_page = 5
        total_pages = (len(all_categories) + items_per_page - 1) // items_per_page
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        categories = all_categories[start_idx:end_idx]

        # Build category selection keyboard
        keyboard = []
        for category in categories:
            keyboard.append([
                InlineKeyboardButton(
                    f"📁 {category.name}",
                    callback_data=f"edit_cat_{category.id}"
                )
            ])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_edit_category_page_{page-1}"))
            pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_edit_category_page_{page+1}"))
            keyboard.append(pagination_row)

        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_categories")])

        await query.edit_message_text(
            "✏️ Edit Category\n\nSelect a category to edit:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_CATEGORY_SELECT


async def edit_category_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection from the list."""
    query = update.callback_query
    await query.answer()

    # Handle back button
    if query.data == "admin_manage_categories":
        from utils import create_admin_category_menu_keyboard
        await query.edit_message_text(
            "📁 Category Management",
            reply_markup=create_admin_category_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Handle pagination - stay in EDIT_CATEGORY_SELECT state
    if "admin_edit_category_page_" in query.data:
        return await edit_category_start(update, context)

    # Extract category ID from callback data
    category_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        category = session.query(Category).filter_by(id=category_id).first()

        if not category:
            await query.edit_message_text(
                "❌ Category not found.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            return ConversationHandler.END

        context.user_data['edit_category_id'] = category_id

        # Show fields to edit
        keyboard = [
            [InlineKeyboardButton("📦 Name", callback_data="editcat_name")],
            [InlineKeyboardButton("📝 Description", callback_data="editcat_desc")],
            [InlineKeyboardButton("🗑 Delete Category", callback_data="editcat_delete")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="cancel_edit_cat")]
        ]

        await query.edit_message_text(
            f"Editing Category: {category.name}\n"
            f"Description: {category.description or 'No description'}\n\n"
            f"What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_CATEGORY_FIELD


async def edit_category_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection for category editing."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_edit_cat":
        from utils import create_admin_category_menu_keyboard
        await query.edit_message_text(
            "❌ Category edit cancelled.",
            reply_markup=create_admin_category_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "editcat_delete":
        # Delete category only - products and subcategories remain (can be reassigned)
        with get_db_session() as session:
            from database import Cart
            category = session.query(Category).filter_by(id=context.user_data['edit_category_id']).first()
            category_name = category.name

            # Count affected items
            products_count = session.query(Product).filter_by(category_id=category.id).count()
            subcats_count = session.query(Subcategory).filter_by(category_id=category.id).count()

            # Delete cart items and unlink products from category
            products = session.query(Product).filter_by(category_id=category.id).all()
            for product in products:
                session.query(Cart).filter_by(product_id=product.id).delete()
                product.category_id = None

            # Unlink subcategories from category
            subcategories = session.query(Subcategory).filter_by(category_id=category.id).all()
            for subcat in subcategories:
                subcat.category_id = None

            # Delete the category
            session.delete(category)
            session.commit()
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                f"✅ Category '{category_name}' deleted successfully!\n\n"
                f"Note: {products_count} product(s) and {subcats_count} subcategory(ies) "
                f"remain and can be reassigned to another category.",
                reply_markup=create_admin_category_menu_keyboard()
            )

        context.user_data.clear()
        return ConversationHandler.END

    context.user_data['edit_category_field'] = query.data.split("_")[1]
    field = context.user_data['edit_category_field']

    # Get current category data to show old value
    with get_db_session() as session:
        category = session.query(Category).filter_by(id=context.user_data['edit_category_id']).first()

        if not category:
            await query.edit_message_text(
                "❌ Category not found.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        if field == 'name':
            prompt = f"📦 Current name: {category.name}\n\nEnter new category name:"
        elif field == 'desc':
            prompt = f"📝 Current description:\n{category.description or 'No description'}\n\nEnter new category description (or type 'skip' to remove):"
        else:
            await query.edit_message_text(
                "❌ Unknown field.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit_cat")]]
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(cancel_keyboard)
    )
    return EDIT_CATEGORY_VALUE


async def edit_category_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new value input for edited category field."""
    field = context.user_data['edit_category_field']
    new_value = update.message.text

    with get_db_session() as session:
        category = session.query(Category).filter_by(id=context.user_data['edit_category_id']).first()

        if field == 'name':
            category.name = new_value
        elif field == 'desc':
            category.description = "" if new_value.lower() == 'skip' else new_value

        session.commit()

        keyboard = [
            [InlineKeyboardButton("✏️ Edit Another Category", callback_data="admin_edit_category")],
            [InlineKeyboardButton("🔙 Back to Category Menu", callback_data="admin_manage_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"✅ Category {field} updated successfully!",
            reply_markup=reply_markup
        )

    context.user_data.clear()
    return ConversationHandler.END


# ==================== SUBCATEGORY EDIT FLOW ====================

async def edit_subcategory_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start subcategory edit flow - show paginated subcategory list."""
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

    with get_db_session() as session:
        # Get all subcategories with their parent categories
        all_subcategories = session.query(Subcategory).order_by(Subcategory.id).all()

        if not all_subcategories:
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                "❌ No subcategories found. Please create a subcategory first.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            return ConversationHandler.END

        # Pagination settings
        items_per_page = 5
        total_pages = (len(all_subcategories) + items_per_page - 1) // items_per_page
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        subcategories = all_subcategories[start_idx:end_idx]

        # Build subcategory selection keyboard
        keyboard = []
        for subcategory in subcategories:
            category = session.query(Category).filter_by(id=subcategory.category_id).first() if subcategory.category_id else None
            category_label = category.name if category else "No Category"
            keyboard.append([
                InlineKeyboardButton(
                    f"📂 {subcategory.name} (in {category_label})",
                    callback_data=f"edit_subcat_{subcategory.id}"
                )
            ])

        # Add pagination buttons if needed
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"admin_edit_subcategory_page_{page-1}"))
            pagination_row.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_edit_subcategory_page_{page+1}"))
            keyboard.append(pagination_row)

        # Add back button
        from utils import create_admin_category_menu_keyboard
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_manage_categories")])

        await query.edit_message_text(
            "✏️ Edit Subcategory\n\nSelect a subcategory to edit:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_SUBCATEGORY_SELECT


async def edit_subcategory_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subcategory selection from the list."""
    query = update.callback_query
    await query.answer()

    # Handle back button
    if query.data == "admin_manage_categories":
        from utils import create_admin_category_menu_keyboard
        await query.edit_message_text(
            "📁 Category Management",
            reply_markup=create_admin_category_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Handle pagination - stay in EDIT_SUBCATEGORY_SELECT state
    if "admin_edit_subcategory_page_" in query.data:
        return await edit_subcategory_start(update, context)

    # Extract subcategory ID from callback data
    subcategory_id = int(query.data.split("_")[2])

    with get_db_session() as session:
        subcategory = session.query(Subcategory).filter_by(id=subcategory_id).first()

        if not subcategory:
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                "❌ Subcategory not found.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            return ConversationHandler.END

        category = session.query(Category).filter_by(id=subcategory.category_id).first() if subcategory.category_id else None
        category_name = category.name if category else "No Category"
        context.user_data['edit_subcategory_id'] = subcategory_id

        # Show fields to edit
        keyboard = [
            [InlineKeyboardButton("📦 Name", callback_data="editsubcat_name")],
            [InlineKeyboardButton("📁 Change Parent Category", callback_data="editsubcat_category")],
            [InlineKeyboardButton("🗑 Delete Subcategory", callback_data="editsubcat_delete")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="cancel_edit_subcat")]
        ]

        await query.edit_message_text(
            f"Editing Subcategory: {subcategory.name}\n"
            f"Parent Category: {category_name}\n\n"
            f"What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_SUBCATEGORY_FIELD


async def edit_subcategory_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection for subcategory editing."""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_edit_subcat":
        from utils import create_admin_category_menu_keyboard
        await query.edit_message_text(
            "❌ Subcategory edit cancelled.",
            reply_markup=create_admin_category_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "editsubcat_delete":
        # Delete subcategory only - products remain (can be reassigned)
        with get_db_session() as session:
            from database import Cart
            subcategory = session.query(Subcategory).filter_by(id=context.user_data['edit_subcategory_id']).first()
            subcategory_name = subcategory.name

            # Get products in this subcategory
            products = session.query(Product).filter_by(subcategory_id=subcategory.id).all()
            products_count = len(products)

            # Delete cart items and unlink products from subcategory
            for product in products:
                session.query(Cart).filter_by(product_id=product.id).delete()
                product.subcategory_id = None

            session.delete(subcategory)
            session.commit()
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                f"✅ Subcategory '{subcategory_name}' deleted successfully!\n\n"
                f"Note: {products_count} product(s) remain and can be reassigned to another subcategory.",
                reply_markup=create_admin_category_menu_keyboard()
            )

        context.user_data.clear()
        return ConversationHandler.END

    if query.data == "editsubcat_category":
        # Show category selection
        with get_db_session() as session:
            categories = session.query(Category).all()

            if not categories:
                from utils import create_admin_category_menu_keyboard
                await query.edit_message_text(
                    "❌ No categories available.",
                    reply_markup=create_admin_category_menu_keyboard()
                )
                context.user_data.clear()
                return ConversationHandler.END

            keyboard = []
            for cat in categories:
                keyboard.append([InlineKeyboardButton(cat.name, callback_data=f"newcat_{cat.id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit_subcat")])

            context.user_data['edit_subcategory_field'] = 'category'

            await query.edit_message_text(
                "📁 Select new parent category:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return EDIT_SUBCATEGORY_VALUE

    context.user_data['edit_subcategory_field'] = query.data.split("_")[1]
    field = context.user_data['edit_subcategory_field']

    # Get current subcategory data to show old value
    with get_db_session() as session:
        subcategory = session.query(Subcategory).filter_by(id=context.user_data['edit_subcategory_id']).first()

        if not subcategory:
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                "❌ Subcategory not found.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

        if field == 'name':
            prompt = f"📦 Current name: {subcategory.name}\n\nEnter new subcategory name:"
        else:
            from utils import create_admin_category_menu_keyboard
            await query.edit_message_text(
                "❌ Unknown field.",
                reply_markup=create_admin_category_menu_keyboard()
            )
            context.user_data.clear()
            return ConversationHandler.END

    cancel_keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit_subcat")]]
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(cancel_keyboard)
    )
    return EDIT_SUBCATEGORY_VALUE


async def edit_subcategory_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new value input for edited subcategory field."""
    field = context.user_data['edit_subcategory_field']

    with get_db_session() as session:
        subcategory = session.query(Subcategory).filter_by(id=context.user_data['edit_subcategory_id']).first()

        if field == 'name':
            new_value = update.message.text
            subcategory.name = new_value
        elif field == 'category':
            # Handle category change via callback
            query = update.callback_query
            await query.answer()

            if query.data == "cancel_edit_subcat":
                from utils import create_admin_category_menu_keyboard
                await query.edit_message_text(
                    "❌ Subcategory edit cancelled.",
                    reply_markup=create_admin_category_menu_keyboard()
                )
                context.user_data.clear()
                return ConversationHandler.END

            new_category_id = int(query.data.split("_")[1])
            subcategory.category_id = new_category_id

        session.commit()

        from utils import create_admin_category_menu_keyboard
        keyboard = [
            [InlineKeyboardButton("✏️ Edit Another Subcategory", callback_data="admin_edit_subcategory")],
            [InlineKeyboardButton("🔙 Back to Category Menu", callback_data="admin_manage_categories")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if field == 'category':
            await query.edit_message_text(
                "✅ Subcategory parent category updated successfully!",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                f"✅ Subcategory {field} updated successfully!",
                reply_markup=reply_markup
            )

    context.user_data.clear()
    return ConversationHandler.END
