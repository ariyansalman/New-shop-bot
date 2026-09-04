"""Inline keyboard utilities for the Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def create_main_menu_keyboard(lang: str = 'en', is_admin_user: bool = False,
                              has_terms: bool = None, has_referrals: bool = None):
    """The user main menu.

    Two full-width entries lead - Products and Search, the two ways in - and
    the six recurring destinations pair up in the same two-column grid the
    admin panel uses. Terms, Language and the admin entry stay full width
    below them: they are read-once or rarely-used, and putting them at half
    width beside a shopping action invites a mis-tap.

    is_admin_user is passed in rather than looked up here, because this is
    called from a dozen handlers that already know who they are answering.
    The Admin Panel button is hidden for everyone else; the panel's own
    handler refuses non-admins as well, so hiding it is presentation,
    not the permission check.

    has_terms and has_referrals default to the cached store flags (see
    services/store_content.py) so the eleven existing call sites need not
    pass them; tests override them directly.
    """
    from .i18n import t

    if has_terms is None:
        from services import store_content
        has_terms = store_content.has_terms()
    if has_referrals is None:
        from services import store_content
        has_referrals = store_content.has_referrals()

    keyboard = [
        [InlineKeyboardButton(t('main_menu.button.products', lang),
                              callback_data="products")],
        [InlineKeyboardButton(t('main_menu.button.search', lang),
                              callback_data="search")],
    ]

    paired = [
        InlineKeyboardButton(t('main_menu.button.topup', lang), callback_data="topup"),
        InlineKeyboardButton(t('main_menu.button.order_history', lang),
                             callback_data="order_history"),
    ]
    if has_referrals:
        paired.append(InlineKeyboardButton(t('main_menu.button.referral', lang),
                                           callback_data="referral"))
    paired += [
        InlineKeyboardButton(t('main_menu.button.account', lang), callback_data="account"),
        InlineKeyboardButton(t('main_menu.button.availability', lang),
                             callback_data="availability"),
        InlineKeyboardButton(t('main_menu.button.support', lang), callback_data="support"),
    ]
    keyboard += two_column_rows(paired)

    if has_terms:
        keyboard.append([InlineKeyboardButton(t('main_menu.button.terms', lang),
                                              callback_data="terms")])
    keyboard.append([InlineKeyboardButton(t('main_menu.button.language', lang),
                                          callback_data="language")])
    if is_admin_user:
        keyboard.append([InlineKeyboardButton(t('main_menu.button.admin', lang),
                                              callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_terms_menu_keyboard(lang: str = 'en'):
    """Terms & FAQ: one button per page, then Back."""
    from .i18n import t

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t('terms.button.conditions', lang),
                              callback_data="terms_conditions")],
        [InlineKeyboardButton(t('terms.button.faq', lang), callback_data="terms_faq")],
        [InlineKeyboardButton(t('common.back_arrow', lang), callback_data="main_menu")],
    ])


def create_language_keyboard():
    """The language picker: two per row, then Back.

    Every entry is written in its own language and carries its flag, so
    someone who cannot read the current interface can still find theirs -
    which is the whole point of this screen.
    """
    from .languages import LANGUAGES, button_label

    keyboard = two_column_rows([
        InlineKeyboardButton(button_label(language),
                             callback_data=f"set_lang_{language.code}")
        for language in LANGUAGES
    ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def create_back_support_keyboard():
    """Create standard back and support buttons."""
    keyboard = [
        [
            InlineKeyboardButton("🔙 Back", callback_data="back"),
            InlineKeyboardButton("☎️ Support", callback_data="support")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_pagination_keyboard(items, page, total_pages, callback_prefix, back_button=True):
    """Create a paginated keyboard with items."""
    keyboard = []

    # Add item buttons - items should already be a list of button rows
    keyboard.extend(items)

    # Add pagination buttons if needed
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"{callback_prefix}_page_{page-1}"))
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{callback_prefix}_page_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    # Add back and support buttons
    if back_button:
        keyboard.append([
            InlineKeyboardButton("🔙 Back", callback_data="back"),
            InlineKeyboardButton("☎️ Support", callback_data="support")
        ])

    return InlineKeyboardMarkup(keyboard)


def create_product_detail_keyboard(product_id, back_callback="back", in_stock=True):
    """Create keyboard for product details view with Buy Now button.

    A sold-out product says so instead of offering Buy Now. The purchase
    flow already refuses it, but only after the customer has tapped and
    waited - the button should not promise something the next screen takes
    away.
    """
    buy = ("🛒 Buy Now" if in_stock else "❌ Sold Out")
    keyboard = [
        [InlineKeyboardButton(buy, callback_data=f"buy_{product_id}")],
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_callback),
            InlineKeyboardButton("☎️ Support", callback_data="support")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_quantity_keyboard(product_id):
    """Create keyboard for quantity confirmation."""
    keyboard = [
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_purchase")]
    ]
    return InlineKeyboardMarkup(keyboard)


def create_cancel_keyboard():
    """Create a simple cancel button keyboard."""
    keyboard = [[InlineKeyboardButton("☎️ Cancel", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)


def payment_methods_available() -> list:
    """The top-up methods this deployment can actually complete.

    A method is offered only when its credentials are set AND no admin has
    switched it off. Showing a button that cannot work is worse than showing
    nothing: CryptoBot in particular used to accept the tap, write a PENDING
    transaction row, fail against the API, and tell the user to "try again" -
    advice that could never succeed, once per attempt, with a dead row left
    behind each time.
    """
    from services import payment_methods

    return [(s.label, s.callback) for s in payment_methods.available_specs()]


def create_payment_method_keyboard():
    """Create payment method selection keyboard."""
    keyboard = [[InlineKeyboardButton(label, callback_data=data)]
                for label, data in payment_methods_available()]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def create_support_keyboard(support_username, channel_username):
    """Create support page keyboard with contact and community links."""
    keyboard = []

    if support_username:
        keyboard.append([InlineKeyboardButton("📞 Contact support", url=f"https://t.me/{support_username}")])

    if channel_username:
        keyboard.append([InlineKeyboardButton("🫂 Join My Community", url=f"https://t.me/{channel_username}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])

    return InlineKeyboardMarkup(keyboard)


def two_column_rows(buttons):
    """Lay a flat list of buttons out two per row.

    The single place the admin panel's grid is defined, so every menu keeps
    the same shape. A trailing odd button ends up alone on the last row,
    which Telegram renders full width - that reads as intentional, so the
    menus below are ordered to put a standalone item there rather than
    leaving a lopsided pair in the middle.

    Deliberately NOT used for lists of records (products, users, orders,
    transactions). Those carry names and prices of unpredictable length;
    at half width they truncate, and two records side by side make a
    mis-tap on a phone both easy and consequential.
    """
    return [list(buttons[i:i + 2]) for i in range(0, len(buttons), 2)]


def _menu(action_buttons, back_label, back_target):
    """An admin menu: actions in a two-column grid, navigation below it.

    Back stays full width on its own row. It is the one button pressed by
    reflex rather than by reading, so it should never sit half width beside
    an action that changes something.
    """
    keyboard = two_column_rows(action_buttons)
    keyboard.append([InlineKeyboardButton(back_label, callback_data=back_target)])
    return InlineKeyboardMarkup(keyboard)


def create_admin_product_menu_keyboard():
    """Create admin product management menu keyboard."""
    return _menu([
        InlineKeyboardButton("➕ Add Product", callback_data="admin_create_product"),
        InlineKeyboardButton("✏️ Edit Product", callback_data="admin_edit_product"),
        InlineKeyboardButton("🔄 Restock Keys", callback_data="admin_restock_keys"),
        InlineKeyboardButton("📁 Categories", callback_data="admin_manage_categories"),
    ], "🔙 Back", "admin_menu")


def create_admin_category_menu_keyboard():
    """Create admin category management menu keyboard."""
    return _menu([
        # Row of adds, then the matching row of edits, so category and
        # subcategory line up in the same column throughout.
        InlineKeyboardButton("➕ Add Category", callback_data="admin_create_category"),
        InlineKeyboardButton("➕ Add Subcategory", callback_data="admin_create_subcategory"),
        InlineKeyboardButton("✏️ Edit Category", callback_data="admin_edit_category"),
        InlineKeyboardButton("✏️ Edit Subcategory", callback_data="admin_edit_subcategory"),
        InlineKeyboardButton("📋 View Categories", callback_data="admin_view_categories"),
    ], "🔙 Back", "admin_products")


def create_admin_user_menu_keyboard():
    """Create admin user management menu keyboard."""
    return _menu([
        InlineKeyboardButton("👁 View Users", callback_data="admin_view_users"),
    ], "🔙 Back", "admin_menu")


def create_admin_order_menu_keyboard():
    """Create admin order management menu keyboard."""
    return _menu([
        # Read-only pair first, then the pair that changes an order.
        InlineKeyboardButton("📋 All Orders", callback_data="admin_view_orders"),
        InlineKeyboardButton("🚨 Disputes", callback_data="admin_view_disputes"),
        InlineKeyboardButton("✅ Confirm Order", callback_data="admin_confirm_order"),
        InlineKeyboardButton("❌ Cancel Order", callback_data="admin_cancel_order"),
    ], "🔙 Back", "admin_menu")


def create_admin_settings_menu_keyboard():
    """Create admin store settings menu keyboard."""
    return _menu([
        InlineKeyboardButton("💬 Welcome Message", callback_data="admin_welcome_msg"),
        InlineKeyboardButton("🖼 Store Logo", callback_data="admin_store_logo"),
        InlineKeyboardButton("📞 Support Username", callback_data="admin_support_username"),
        InlineKeyboardButton("📢 Channel Username", callback_data="admin_channel_username"),
        InlineKeyboardButton("📜 Terms", callback_data="admin_terms"),
        InlineKeyboardButton("❓ FAQ", callback_data="admin_faq"),
        InlineKeyboardButton("👥 Refer & Earn", callback_data="admin_referral"),
    ], "🔙 Back", "admin_menu")


def create_admin_broadcast_menu_keyboard():
    """Create admin broadcast menu keyboard."""
    return _menu([
        InlineKeyboardButton("💬 Text Only", callback_data="admin_broadcast_text"),
        InlineKeyboardButton("🖼 Image + Text", callback_data="admin_broadcast_image"),
    ], "🔙 Back", "admin_menu")
