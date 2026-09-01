"""Main bot entry point for the Telegram Digital Products Store."""

import logging

from telegram import Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ConversationHandler, PreCheckoutQueryHandler
)
from config import settings, validate_settings, init_sentry
from database.init_data import initialize_database
from handlers import (user_handlers, admin_handlers, payment_handlers,
                      admin_conversations, dispute_handlers, binance_pay_handlers,
                      binance_admin)

# M""M M"""""""`YM M""""""'YMM M"""""`'"""`YM M""""""'YMM MM""""""""`M M""MMMMM""M 
# M  M M  mmmm.  M M  mmmm. `M M  mm.  mm.  M M  mmmm. `M MM  mmmmmmmM M  MMMMM  M 
# M  M M  MMMMM  M M  MMMMM  M M  MMM  MMM  M M  MMMMM  M M`      MMMM M  MMMMP  M 
# M  M M  MMMMM  M M  MMMMM  M M  MMM  MMM  M M  MMMMM  M MM  MMMMMMMM M  MMMM' .M 
# M  M M  MMMMM  M M  MMMM' .M M  MMM  MMM  M M  MMMM' .M MM  MMMMMMMM M  MMP' .MM 
# M  M M  MMMMM  M M       .MM M  MMM  MMM  M M       .MM MM        .M M     .dMMM 
# MMMM MMMMMMMMMMM MMMMMMMMMMM MMMMMMMMMMMMMM MMMMMMMMMMM MMMMMMMMMMMM MMMMMMMMMMM 

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def error_handler(update: object, context) -> None:
    """Log unhandled handler exceptions instead of letting them vanish.

    Without this, any exception inside a handler was swallowed by PTB and the
    user was left staring at a spinner with no feedback.
    """
    logger.error("Exception while handling an update", exc_info=context.error)

    if isinstance(context.error, (BadRequest, Forbidden)):
        # Usually "message is not modified" or the user blocked the bot.
        return

    if isinstance(update, Update):
        try:
            if update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Something went wrong. Please try again.", show_alert=True
                )
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again or contact support."
                )
        except Exception:
            pass


def build_application(post_init=None):
    """Build the Telegram Application with every handler and job registered.

    Split out of main() so app.py can build the bot, attach the CryptoBot
    webhook server to the same process, and then run it.

    Args:
        post_init: optional async callable awaited once the event loop is
            running, before polling starts. app.py uses it to launch the HTTP
            server with a live loop to schedule notifications onto.
    """
    builder = Application.builder().token(settings.BOT_TOKEN)
    if post_init is not None:
        builder = builder.post_init(post_init)
    application = builder.build()

    # Register command handlers
    application.add_handler(CommandHandler("start", user_handlers.start_command))
    application.add_handler(CommandHandler("admin", admin_handlers.admin_command))

    # Register conversation handlers for multi-step flows

    # Top-up conversation
    topup_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(payment_handlers.topup_start, pattern="^topup$"),
            # Re-enters the conversation at the order-id input state.
            CallbackQueryHandler(binance_pay_handlers.binance_submit_start,
                                 pattern="^binance_submit_"),
        ],
        states={
            payment_handlers.AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.topup_amount)],
            payment_handlers.METHOD: [
                CallbackQueryHandler(payment_handlers.payment_method_crypto, pattern="^pay_crypto$"),
                CallbackQueryHandler(payment_handlers.payment_method_card, pattern="^pay_card$"),
                CallbackQueryHandler(binance_pay_handlers.payment_method_binance, pattern="^pay_binance$"),
            ],
            # "Submit Order ID" opens a plain text-input state: the user
            # sends the Binance id and that submission itself starts
            # verification. Deliberately no Verify/Cancel buttons here.
            binance_pay_handlers.BINANCE_ORDER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               binance_pay_handlers.binance_order_id_received),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_topup, pattern="^cancel$"),
            CallbackQueryHandler(payment_handlers.cancel_topup)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(topup_conv_handler)

    # Telegram Payments (Card) handlers — confirmation arrives via the bot's update
    # polling, not a separate job: approve the pre-checkout, then credit on success.
    application.add_handler(PreCheckoutQueryHandler(payment_handlers.precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handlers.successful_payment_callback))

    # Product creation conversation
    create_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_product_start, pattern="^admin_create_product$")],
        states={
            admin_conversations.PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_name),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_desc),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_price),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_TYPE: [
                CallbackQueryHandler(admin_conversations.product_type, pattern="^type_"),
                CallbackQueryHandler(admin_conversations.product_type, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_CATEGORY: [
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cat_"),
                CallbackQueryHandler(admin_conversations.product_category, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_SUBCATEGORY: [
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^subcat_"),
                CallbackQueryHandler(admin_conversations.product_subcategory, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_IMAGE: [
                MessageHandler(filters.PHOTO | filters.TEXT, admin_conversations.product_image),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_DOWNLOAD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_download_link),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
            admin_conversations.PRODUCT_KEYS: [
                MessageHandler(filters.Document.ALL, admin_conversations.product_keys),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.product_keys),
                CallbackQueryHandler(admin_conversations.cancel_product_creation, pattern="^cancel_product$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_product_creation),
            CallbackQueryHandler(admin_conversations.cancel_product_creation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_product_conv)

    # Product edit conversation
    edit_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_product_start, pattern="^admin_edit_product$")],
        states={
            admin_conversations.EDIT_SELECT_PRODUCT: [
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^edit_prod_"),
                CallbackQueryHandler(admin_conversations.edit_select_product, pattern="^admin_edit_product_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_products$")
            ],
            admin_conversations.EDIT_SELECT_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^edit_"),
                CallbackQueryHandler(admin_conversations.edit_select_field, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_NEW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_new_value),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodcat_"),
                CallbackQueryHandler(admin_conversations.edit_new_value, pattern="^newprodsubcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit$")
            ],
            admin_conversations.EDIT_IMAGE_VALUE: [
                MessageHandler(filters.PHOTO, admin_conversations.edit_image_value),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^remove_product_image$"),
                CallbackQueryHandler(admin_conversations.edit_image_value, pattern="^cancel_edit$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_product_conv)

    # Category creation conversation
    create_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_category_start, pattern="^admin_create_category$")],
        states={
            admin_conversations.CATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_name)],
            admin_conversations.CATEGORY_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.category_desc)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_category_conv)

    # Subcategory creation conversation
    create_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.create_subcategory_start, pattern="^admin_create_subcategory$")],
        states={
            admin_conversations.SUBCATEGORY_CATEGORY: [
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^subcat_cat_"),
                CallbackQueryHandler(admin_conversations.subcategory_category, pattern="^cancel_subcat$")
            ],
            admin_conversations.SUBCATEGORY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.subcategory_name)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(create_subcategory_conv)

    # Category edit conversation
    edit_category_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_category_start, pattern="^admin_edit_category$")],
        states={
            admin_conversations.EDIT_CATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^edit_cat_"),
                CallbackQueryHandler(admin_conversations.edit_category_select, pattern="^admin_edit_category_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_CATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^editcat_"),
                CallbackQueryHandler(admin_conversations.edit_category_field, pattern="^cancel_edit_cat$")
            ],
            admin_conversations.EDIT_CATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_category_value),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_cat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_category_conv)

    # Subcategory edit conversation
    edit_subcategory_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.edit_subcategory_start, pattern="^admin_edit_subcategory$")],
        states={
            admin_conversations.EDIT_SUBCATEGORY_SELECT: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^edit_subcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_select, pattern="^admin_edit_subcategory_page_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^admin_manage_categories$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_FIELD: [
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^editsubcat_"),
                CallbackQueryHandler(admin_conversations.edit_subcategory_field, pattern="^cancel_edit_subcat$")
            ],
            admin_conversations.EDIT_SUBCATEGORY_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.edit_subcategory_value),
                CallbackQueryHandler(admin_conversations.edit_subcategory_value, pattern="^newcat_"),
                CallbackQueryHandler(admin_conversations.cancel_conversation, pattern="^cancel_edit_subcat$")
            ],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(edit_subcategory_conv)

    # Support username configuration conversation
    config_support_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_support_username, pattern="^admin_support_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_support_conv)

    # Channel username configuration conversation
    config_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_channel_username, pattern="^admin_channel_username$")],
        states={
            admin_conversations.SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.setting_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_conversation),
            CallbackQueryHandler(admin_conversations.cancel_conversation)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_channel_conv)

    # Welcome message configuration conversation
    config_welcome_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_welcome_message, pattern="^admin_welcome_msg$")],
        states={
            admin_conversations.WELCOME_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.welcome_message_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_welcome_conv)

    # Store logo configuration conversation
    config_logo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.config_store_logo, pattern="^admin_store_logo$")],
        states={
            admin_conversations.STORE_LOGO: [MessageHandler(filters.PHOTO, admin_conversations.store_logo_value)],
        },
        fallbacks=[
            MessageHandler(filters.COMMAND, admin_conversations.cancel_settings),
            CallbackQueryHandler(admin_conversations.cancel_settings, pattern="^cancel$")
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(config_logo_conv)

    # Text-only broadcast conversation
    broadcast_text_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_text_start, pattern="^admin_broadcast_text$")],
        states={
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_text_message)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_text_conv)

    # Image + Text broadcast conversation
    broadcast_image_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_conversations.broadcast_image_start, pattern="^admin_broadcast_image$")],
        states={
            admin_conversations.BROADCAST_IMAGE: [
                MessageHandler(filters.PHOTO, admin_conversations.broadcast_image_photo)
            ],
            admin_conversations.BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_conversations.broadcast_image_text)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_conversations.cancel_broadcast, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, admin_conversations.cancel_broadcast)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(broadcast_image_conv)

    # Dispute conversation
    dispute_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dispute_handlers.open_dispute_start, pattern="^open_dispute_")],
        states={
            dispute_handlers.DISPUTE_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dispute_handlers.dispute_reason_received)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(dispute_handlers.dispute_cancel, pattern="^cancel$"),
            MessageHandler(filters.COMMAND, dispute_handlers.dispute_cancel)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(dispute_conv)

    # Direct purchase conversation (Buy Now flow)
    purchase_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_handlers.buy_product_start, pattern="^buy_")],
        states={
            payment_handlers.PURCHASE_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handlers.purchase_quantity_input),
                CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"),
            MessageHandler(filters.COMMAND, payment_handlers.cancel_purchase)
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(purchase_conv)

    # Register callback query handlers
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(user_handlers.set_language_callback, pattern="^set_lang_"))
    application.add_handler(CallbackQueryHandler(user_handlers.main_menu_callback, pattern="^back$"))  # Back button goes to main menu
    application.add_handler(CallbackQueryHandler(user_handlers.back_to_products_callback, pattern="^back_to_products$"))
    application.add_handler(CallbackQueryHandler(user_handlers.products_callback, pattern="^products(_page_\\d+)?$"))
    # Product-list paging (previously emitted "products_page_N", which was
    # swallowed by the category-list handler above).
    application.add_handler(CallbackQueryHandler(user_handlers.products_page_callback, pattern="^plist_"))
    application.add_handler(CallbackQueryHandler(user_handlers.category_callback, pattern="^category_"))
    application.add_handler(CallbackQueryHandler(user_handlers.subcategory_callback, pattern="^subcategory_"))
    application.add_handler(CallbackQueryHandler(user_handlers.product_callback, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(user_handlers.availability_callback, pattern="^availability$"))
    application.add_handler(CallbackQueryHandler(user_handlers.support_callback, pattern="^support$"))
    application.add_handler(CallbackQueryHandler(user_handlers.order_history_callback, pattern="^order_history$"))
    application.add_handler(CallbackQueryHandler(user_handlers.user_order_detail_callback, pattern="^user_order_detail_"))

    # Purchase confirmation and cancellation handlers
    application.add_handler(CallbackQueryHandler(payment_handlers.confirm_purchase, pattern="^confirm_purchase_"))
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_purchase, pattern="^cancel_purchase$"))

    # Global cancel handler for payment pages (outside conversation)
    application.add_handler(CallbackQueryHandler(payment_handlers.cancel_payment_page, pattern="^cancel$"))

    # Admin callback handlers
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_menu_callback, pattern="^admin_menu$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_action_log_callback, pattern="^admin_action_log$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_products_callback, pattern="^admin_products$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_restock_keys_callback, pattern="^admin_restock_keys$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_manage_categories_callback, pattern="^admin_manage_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_categories_callback, pattern="^admin_view_categories$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_users_callback, pattern="^admin_view_users(_page_\\d+)?$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_user_detail_callback, pattern="^view_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_ban_user_callback, pattern="^ban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_unban_user_callback, pattern="^unban_user_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_view_orders_callback, pattern="^admin_view_orders(_page_\\d+)?$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_order_menu, pattern="^admin_confirm_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_menu, pattern="^admin_cancel_order$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_confirm_payment_callback, pattern="^confirm_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_payment_callback, pattern="^cancel_payment_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_order_detail_callback, pattern="^view_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_complete_order_callback, pattern="^complete_order_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_cancel_order_callback, pattern="^cancel_order_"))
    # "Reactivate Order" was rendered as a button but had no handler at all.
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_reactivate_order_callback, pattern="^reactivate_order_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_view_disputes_callback, pattern="^admin_view_disputes$"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_dispute_detail_callback, pattern="^admin_dispute_detail_"))
    application.add_handler(CallbackQueryHandler(dispute_handlers.admin_resolve_dispute_callback, pattern="^resolve_dispute_"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_users_callback, pattern="^admin_users$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_orders_callback, pattern="^admin_orders$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_settings_callback, pattern="^admin_settings$"))
    application.add_handler(CallbackQueryHandler(admin_handlers.admin_broadcast_callback, pattern="^admin_broadcast$"))
    # Inert "Page x/y" labels: answer them so the client stops showing a spinner.
    application.add_handler(CallbackQueryHandler(admin_handlers.noop_callback, pattern="^noop$"))

    # Restock keys conversation handler
    # filters.User(user_id=list(...)) so every configured admin (not just the
    # single legacy ADMIN_TELEGRAM_ID) can restock - the entry point already
    # checks is_admin(), but the follow-up MessageHandlers need their own
    # filter since a plain text/document message is not a callback query.
    admin_ids = list(settings.ADMIN_IDS)
    restock_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_handlers.admin_select_product_restock_callback, pattern="^select_product_")],
        states={
            admin_handlers.WAITING_FOR_KEYS: [
                MessageHandler(filters.Document.ALL & filters.User(user_id=admin_ids), admin_handlers.handle_restock_keys_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(user_id=admin_ids), admin_handlers.handle_restock_keys_paste),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(admin_handlers.cancel_restock, pattern="^cancel_restock$"),
            CommandHandler("cancel", admin_handlers.cancel_restock, filters=filters.User(user_id=admin_ids)),
        ],
    )
    application.add_handler(restock_conv)

    # Binance "Cancel Order" lives outside the top-up conversation: the
    # conversation has already ended by the time the checkout is on screen.
    application.add_handler(CallbackQueryHandler(
        binance_pay_handlers.binance_cancel_order, pattern="^binance_cancel_"))

    # Binance admin panel. Every one of these re-checks is_admin() itself -
    # the pattern is not the authorization.
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_menu, pattern="^binadmin_menu$"))
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_toggle, pattern="^binadmin_toggle$"))
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_test, pattern="^binadmin_test$"))
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_monitor, pattern="^binadmin_mon_"))
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_retry, pattern="^binadmin_retry_"))
    application.add_handler(CallbackQueryHandler(
        binance_admin.binance_admin_close, pattern="^binadmin_close_"))

    # Schedule background jobs
    job_queue = application.job_queue

    # Payment checking jobs
    job_queue.run_repeating(
        payment_handlers.check_pending_payments,
        interval=settings.PAYMENT_CHECK_INTERVAL,
        first=10
    )
    job_queue.run_repeating(
        payment_handlers.check_expired_payments,
        interval=60,
        first=30
    )

    # Binance top-ups whose id was submitted but did not resolve first time.
    # Same queue as the pollers above - no second scheduler. Far slower than
    # the CryptoBot poll because the Pay history endpoint is Weight(UID) 3000.
    if binance_pay_handlers.binance_pay_available():
        logger.info("Scheduling Binance verification retry job (every %ss)",
                    settings.BINANCE_VERIFY_RETRY_INTERVAL)
        job_queue.run_repeating(
            binance_pay_handlers.retry_pending_binance_payments,
            interval=settings.BINANCE_VERIFY_RETRY_INTERVAL,
            first=settings.BINANCE_VERIFY_RETRY_INTERVAL,
        )

    # Availability broadcast job - runs every 12 hours (43200 seconds)
    # NOTE: this used to fire 10 seconds after every restart, so each restart
    # blasted the whole user base. First run is now one full interval away.
    logger.info("Scheduling availability broadcast job (every 12 hours)")
    job_queue.run_repeating(
        payment_handlers.broadcast_availability_to_all_users,
        interval=43200,  # 12 hours in seconds
        first=43200
    )

    # Global error handler (must be registered last)
    application.add_error_handler(error_handler)

    return application


def main():
    """Initialize and start the bot (polling only, no webhook server)."""
    try:
        validate_settings()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    init_sentry()

    try:
        initialize_database()
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return

    # Load the Binance kill switch once, before any update is served, so
    # binance_pay_available() never has to touch the database from the
    # event loop.
    binance_pay_handlers.refresh_admin_toggle()

    application = build_application()

    logger.info("Bot started successfully!")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
