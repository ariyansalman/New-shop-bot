"""Payment and wallet management handlers."""

import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ContextTypes, ConversationHandler
from database import (
    get_db_session, User, Transaction, Order, OrderItem, Product,
    ProductKey, TransactionStatus, OrderStatus, PaymentMethod, ProductType
)
from utils import (
    format_price, validate_amount, to_money,
    create_cancel_keyboard, create_payment_method_keyboard,
    create_quantity_keyboard, create_main_menu_keyboard,
    calculate_expiry_time, notify_admin, check_user_banned_async,
    t, DEFAULT_LANG,
)
from config.settings import settings as app_settings
from services.crypto_bot import CryptoBotService

logger = logging.getLogger(__name__)

# Conversation states for top-up
AMOUNT, METHOD = range(2)

# Conversation states for direct purchase
PURCHASE_QUANTITY = 10

# Hard ceiling on a single purchase, independent of what the client sends back
MAX_PURCHASE_QUANTITY = 1000


def _get_user_lang(telegram_id: int) -> str:
    """Look up a user's language preference for a message shown before (or
    without) an existing get_db_session() block already having the row.
    """
    with get_db_session() as session:
        lang = session.query(User.language).filter_by(telegram_id=telegram_id).scalar()
    return lang or DEFAULT_LANG


async def topup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the wallet top-up flow."""
    query = update.callback_query
    await query.answer()

    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text("⛔ You have been banned from using this bot.")
        return ConversationHandler.END

    message = "💬 Please reply the amount USD you want to fund your wallet.\nExample: 100"

    await query.edit_message_text(
        message,
        reply_markup=create_cancel_keyboard()
    )

    return AMOUNT


async def topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle amount input for wallet top-up."""
    amount_str = update.message.text

    # Validate amount
    is_valid, amount, error_msg = validate_amount(amount_str)

    if not is_valid:
        await update.message.reply_text(
            f"❌ {error_msg}\n\nPlease enter a valid amount:",
            reply_markup=create_cancel_keyboard()
        )
        return AMOUNT

    # Store amount in context
    context.user_data['topup_amount'] = amount

    # Show payment method selection directly (skip crypto selection for CryptoBot)
    message = f"💰 Amount: ${amount:.2f}\n\n💬 Please choose a payment method:"

    await update.message.reply_text(
        message,
        reply_markup=create_payment_method_keyboard()
    )

    return METHOD


async def payment_method_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Crypto Wallet payment method selection."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

        if not user:
            await query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

        # Check if user already has a pending CryptoBot transaction
        existing_pending = session.query(Transaction).filter_by(
            user_id=user.id,
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING
        ).first()

        if existing_pending:
            # Show full payment details for the existing pending order
            # Extract pay_url from crypto_address (format: "invoice_id|pay_url")
            pay_url = None
            if existing_pending.crypto_address and "|" in existing_pending.crypto_address:
                _, pay_url = existing_pending.crypto_address.split("|", 1)
            elif existing_pending.crypto_address and existing_pending.crypto_address.startswith("http"):
                pay_url = existing_pending.crypto_address

            if not pay_url:
                # An InlineKeyboardButton with an invalid url raises BadRequest.
                await query.edit_message_text(
                    "⚠️ You have a pending payment but its payment link is missing.\n"
                    "Please wait for it to expire, or contact support.",
                    reply_markup=create_main_menu_keyboard()
                )
                return ConversationHandler.END

            message = f"""⚠️ You already have a pending CryptoBot payment!

💬 CryptoBot Payment

💰 Amount: {format_price(existing_pending.amount)}
🆔 Order ID: #{existing_pending.id}

Click the button below to complete your payment. You can pay with ANY cryptocurrency supported by CryptoBot:

✅ BTC (Bitcoin)
✅ TON (Toncoin)
✅ USDT (TRC20, TON)
✅ USDC (TRC20, TON)
✅ ETH (Ethereum)
✅ LTC (Litecoin)
✅ BNB (Binance Coin)
✅ TRX (Tron)
And many more!

The system will automatically verify and add ${existing_pending.amount:.2f} to your balance as soon as your payment is confirmed.

⏰ Expires: {existing_pending.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC') if existing_pending.expires_at else 'N/A'}

You cannot create a new order until this one is completed or expired."""

            # Create keyboard with payment button
            keyboard = [
                [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # No parse_mode: the text contains '#', '$' and '_' which Telegram
            # rejects as malformed Markdown entities.
            await query.edit_message_text(message, reply_markup=reply_markup)
            return ConversationHandler.END

        # Create transaction record
        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.CRYPTO_WALLET,
            status=TransactionStatus.PENDING,
            expires_at=calculate_expiry_time(app_settings.PAYMENT_EXPIRY_HOURS)
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)

        # Generate payment invoice in USD (accepts any cryptocurrency)
        crypto_service = CryptoBotService()
        payment_address = crypto_service.generate_payment_address(
            usd_amount,
            transaction.id
        )

        if not payment_address:
            transaction.status = TransactionStatus.FAILED
            session.commit()
            await query.edit_message_text("❌ Failed to generate payment invoice. Please try again.")
            return ConversationHandler.END

        # Update transaction with crypto address (format: "invoice_id|pay_url")
        transaction.crypto_address = payment_address
        session.commit()

        # Extract pay_url from payment_address
        if "|" in payment_address:
            invoice_id, pay_url = payment_address.split("|", 1)
            logger.info("Invoice created: ID=%s, URL=%s", invoice_id, pay_url)
        else:
            # Fallback for unexpected format
            pay_url = payment_address

        # Show payment instructions
        message = f"""💬 CryptoBot Payment

💰 Amount: {format_price(usd_amount)}
🆔 Order ID: #{transaction.id}

Click the button below to open the payment page. You can pay with ANY cryptocurrency supported by CryptoBot:

✅ BTC (Bitcoin)
✅ TON (Toncoin)
✅ USDT (TRC20, TON)
✅ USDC (TRC20, TON)
✅ ETH (Ethereum)
✅ LTC (Litecoin)
✅ BNB (Binance Coin)
✅ TRX (Tron)
And many more!

The system will automatically verify and add ${usd_amount:.2f} to your balance as soon as your payment is confirmed.

⏰ This order will expire in 30 Minutes."""

        # Create keyboard with payment button
        keyboard = [
            [InlineKeyboardButton("💳 Pay with Any Crypto", url=pay_url)],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup)

    return ConversationHandler.END


async def payment_method_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Card payment via Telegram Payments (native sendInvoice flow)."""
    query = update.callback_query
    await query.answer()

    usd_amount = context.user_data.get('topup_amount', 0)
    user_id = update.effective_user.id

    provider_token = app_settings.TELEGRAM_PROVIDER_TOKEN
    if not provider_token:
        await query.edit_message_text(
            "❌ Card payments are not configured yet.\n\nPlease choose another payment method or contact support.",
            reply_markup=create_cancel_keyboard()
        )
        return ConversationHandler.END

    if usd_amount <= 0:
        await query.edit_message_text("❌ Invalid amount. Please start the top-up again.")
        return ConversationHandler.END

    # Create a pending transaction; its id is carried in the invoice payload.
    # Card transactions have no expires_at: confirmation arrives via Telegram's
    # successful_payment update, so the expiry job should not touch them.
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

        transaction = Transaction(
            user_id=user.id,
            amount=usd_amount,
            payment_method=PaymentMethod.CARD,
            status=TransactionStatus.PENDING
        )
        session.add(transaction)
        session.commit()
        session.refresh(transaction)
        transaction_id = transaction.id

    # Replace the method-selection message with a short notice, then send the invoice.
    try:
        await query.edit_message_text(
            f"""💳 Card Payment

💰 Amount: {format_price(usd_amount)}
🆔 Order ID: #{transaction_id}

Please complete the secure card payment below 👇"""
        )
    except Exception:
        pass

    # Telegram expects the price in the smallest currency unit (e.g. cents for USD).
    # usd_amount is already cent-quantized (validate_amount -> to_money), so
    # *100 is an exact integer value; to_money() here is just defense in depth.
    prices = [LabeledPrice(label="Wallet Top-up", amount=int(to_money(usd_amount) * 100))]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Wallet Top-up",
        description=f"Add {format_price(usd_amount)} to your wallet balance.",
        payload=f"topup_{transaction_id}",
        provider_token=provider_token,
        currency=app_settings.PAYMENT_CURRENCY,
        prices=prices,
        start_parameter=f"topup-{transaction_id}"
    )

    return ConversationHandler.END


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve the pre-checkout query if it maps to a valid pending card top-up."""
    query = update.pre_checkout_query
    payload = query.invoice_payload or ""

    transaction_id = None
    if payload.startswith("topup_"):
        try:
            transaction_id = int(payload.split("_", 1)[1])
        except (ValueError, IndexError):
            transaction_id = None

    is_valid = False
    if transaction_id is not None:
        with get_db_session() as session:
            transaction = session.query(Transaction).filter_by(
                id=transaction_id,
                payment_method=PaymentMethod.CARD
            ).first()
            # Allow if not already credited (PENDING, or EXPIRED for a late-but-honoured pay).
            if transaction and transaction.status != TransactionStatus.COMPLETED:
                is_valid = True

    if is_valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="This payment order is no longer valid. Please start a new top-up."
        )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Credit the wallet once Telegram confirms a successful card payment."""
    payment = update.message.successful_payment
    payload = payment.invoice_payload or ""

    if not payload.startswith("topup_"):
        return

    try:
        transaction_id = int(payload.split("_", 1)[1])
    except (ValueError, IndexError):
        return

    notif = None
    with get_db_session() as session:
        transaction = session.query(Transaction).filter_by(
            id=transaction_id,
            payment_method=PaymentMethod.CARD
        ).first()

        if not transaction:
            return

        # Idempotency guard: never double-credit a completed transaction.
        if transaction.status == TransactionStatus.COMPLETED:
            return

        transaction.status = TransactionStatus.COMPLETED
        transaction.completed_at = datetime.utcnow()
        # Store Telegram's charge id in crypto_address for reference.
        transaction.crypto_address = f"tg_charge:{payment.telegram_payment_charge_id}"

        user = session.query(User).filter_by(id=transaction.user_id).first()
        if user:
            user.wallet_balance = to_money(user.wallet_balance + transaction.amount)
            session.commit()
            notif = {
                'telegram_id': user.telegram_id,
                'amount': transaction.amount,
                'new_balance': user.wallet_balance,
                'transaction_id': transaction.id
            }

    if not notif:
        return

    user_message = f"""✅ Payment Confirmed!

💳 Method: Card
💰 Amount: {format_price(notif['amount'])}
🔄 Your new wallet balance: {format_price(notif['new_balance'])}

Thank you for your payment!"""

    await update.message.reply_text(user_message, reply_markup=create_main_menu_keyboard())

    admin_message = f"""💳 New Card Payment Received

👤 User ID: {notif['telegram_id']}
💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}
🔄 Payment Method: Card"""

    await notify_admin(context, admin_message)


async def cancel_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the top-up process (during conversation)."""
    query = update.callback_query
    await query.answer()

    from utils import create_back_support_keyboard

    await query.edit_message_text(
        "❌ Top-up cancelled.",
        reply_markup=create_back_support_keyboard()
    )

    # Clear user data
    context.user_data.clear()

    return ConversationHandler.END


async def cancel_payment_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel from payment instruction page (outside conversation)."""
    query = update.callback_query
    await query.answer()

    from utils import create_main_menu_keyboard

    await query.edit_message_text(
        "❌ Payment cancelled. You can try again anytime.",
        reply_markup=create_main_menu_keyboard()
    )

    # Clear user data
    context.user_data.clear()


async def check_pending_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to check pending payment transactions (non-blocking)."""
    import asyncio

    def _check_and_process_payments_sync():
        """Synchronous database operations run in thread pool."""
        payment_notifications = []

        with get_db_session() as session:
            # Card top-ups are confirmed by Telegram's successful_payment update,
            # so polling them here is pointless work.
            pending_transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING,
                payment_method=PaymentMethod.CRYPTO_WALLET
            ).all()

            crypto_service = CryptoBotService()

            for transaction in pending_transactions:
                # Check if transaction has expired
                if transaction.expires_at and datetime.utcnow() > transaction.expires_at:
                    continue  # Will be handled by check_expired_payments

                is_paid = crypto_service.check_payment_status(
                    transaction.crypto_address, transaction.amount
                )

                if is_paid:
                    # Re-fetch under a row lock and re-check status: the
                    # CryptoBot webhook runs in this same process and could be
                    # crediting this exact transaction right now. Without the
                    # lock, both paths could read status=PENDING here and
                    # credit the wallet twice for one payment.
                    locked_txn = session.query(Transaction).filter_by(
                        id=transaction.id, status=TransactionStatus.PENDING
                    ).with_for_update().first()
                    if not locked_txn:
                        continue  # already handled by the webhook

                    locked_txn.status = TransactionStatus.COMPLETED
                    locked_txn.completed_at = datetime.utcnow()

                    # Update user wallet balance
                    user = session.query(User).filter_by(id=locked_txn.user_id).first()
                    if user:
                        user.wallet_balance = to_money(user.wallet_balance + locked_txn.amount)
                        session.commit()

                        # Store notification data
                        payment_notifications.append({
                            'user_telegram_id': user.telegram_id,
                            'amount': locked_txn.amount,
                            'new_balance': user.wallet_balance,
                            'transaction_id': locked_txn.id,
                            'payment_method': locked_txn.payment_method.value
                        })

        return payment_notifications

    # Run blocking database operations in thread pool
    notifications = await asyncio.to_thread(_check_and_process_payments_sync)

    # Send notifications asynchronously
    for notif in notifications:
        # Notify user
        user_message = f"""✅ Payment Confirmed!

💰 Amount: {format_price(notif['amount'])}
🔄 Your new wallet balance: {format_price(notif['new_balance'])}

Thank you for your payment!"""

        try:
            await context.bot.send_message(
                chat_id=notif['user_telegram_id'],
                text=user_message
            )
        except Exception:
            pass

        # Notify admin
        admin_message = f"""💰 New Payment Received

👤 User ID: {notif['user_telegram_id']}
💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}
🔄 Payment Method: {notif['payment_method']}"""

        await notify_admin(context, admin_message)


async def check_expired_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to mark expired payment transactions (non-blocking)."""
    import asyncio

    def _check_expired_sync():
        """Synchronous database operations run in thread pool."""
        expired_notifications = []

        with get_db_session() as session:
            pending_transactions = session.query(Transaction).filter_by(
                status=TransactionStatus.PENDING
            ).all()

            for transaction in pending_transactions:
                if transaction.expires_at and datetime.utcnow() > transaction.expires_at:
                    # Mark as expired
                    transaction.status = TransactionStatus.EXPIRED

                    # Get user info for notification
                    user = session.query(User).filter_by(id=transaction.user_id).first()
                    if user:
                        expired_notifications.append({
                            'telegram_id': user.telegram_id,
                            'amount': transaction.amount,
                            'transaction_id': transaction.id
                        })

                    session.commit()

        return expired_notifications

    # Run blocking database operations in thread pool
    notifications = await asyncio.to_thread(_check_expired_sync)

    # Send notifications asynchronously
    for notif in notifications:
        message = f"""⏰ Payment Order Expired

💰 Amount: {format_price(notif['amount'])}
📝 Transaction ID: #{notif['transaction_id']}

Your payment order has expired. Please create a new top-up request if you still want to fund your wallet."""

        try:
            await context.bot.send_message(
                chat_id=notif['telegram_id'],
                text=message
            )
        except Exception:
            # User may have blocked the bot
            pass


async def buy_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the direct purchase flow - ask for quantity."""
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id

    # Check if user is banned
    if await check_user_banned_async(telegram_id):
        lang = _get_user_lang(telegram_id)
        await query.edit_message_text(t('purchase.banned', lang))
        return ConversationHandler.END

    # Extract product_id from callback data (format: buy_123)
    try:
        product_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return ConversationHandler.END

    with get_db_session() as session:
        lang = session.query(User.language).filter_by(telegram_id=telegram_id).scalar() or DEFAULT_LANG
        product = session.query(Product).filter_by(id=product_id).first()

        if not product:
            await query.edit_message_text("❌ Product not found.")
            return ConversationHandler.END

        if not product.is_active:
            await query.edit_message_text("❌ This product is no longer available.")
            return ConversationHandler.END

        if product.stock_count == 0:
            await query.edit_message_text("❌ This product is out of stock.")
            return ConversationHandler.END

        # Store product info in context for later
        context.user_data['purchase_product_id'] = product_id
        context.user_data['purchase_product_name'] = product.name
        context.user_data['purchase_product_price'] = product.price
        context.user_data['purchase_product_stock'] = product.stock_count
        context.user_data['purchase_product_type'] = product.product_type

        # For file products, quantity is always 1
        if product.product_type == ProductType.FILE:
            context.user_data['purchase_quantity'] = 1
            # Skip quantity input, go straight to confirmation
            return await show_purchase_confirmation(update, context)

        # For key products, ask for quantity
        message = t(
            'purchase.quantity_prompt', lang,
            product_name=product.name,
            price=format_price(product.price),
            stock=product.stock_count,
        )

        # If coming from a photo message, delete it and create new text message
        if query.message.photo:
            await query.message.delete()
            await query.message.reply_text(
                message,
                reply_markup=create_quantity_keyboard(product_id)
            )
        else:
            await query.edit_message_text(
                message,
                reply_markup=create_quantity_keyboard(product_id)
            )

        return PURCHASE_QUANTITY


async def purchase_quantity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quantity input for direct purchase."""
    quantity_str = update.message.text.strip()

    # Validate quantity
    try:
        quantity = int(quantity_str)
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    product_stock = context.user_data.get('purchase_product_stock', 0)

    if quantity < 1:
        await update.message.reply_text(
            "❌ Quantity must be at least 1.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    if quantity > MAX_PURCHASE_QUANTITY:
        await update.message.reply_text(
            f"❌ Quantity must be at most {MAX_PURCHASE_QUANTITY}.",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    if quantity > product_stock:
        await update.message.reply_text(
            f"❌ Not enough stock. Maximum available: {product_stock}",
            reply_markup=create_quantity_keyboard(context.user_data.get('purchase_product_id', 0))
        )
        return PURCHASE_QUANTITY

    # Store quantity and show confirmation
    context.user_data['purchase_quantity'] = quantity
    return await show_purchase_confirmation(update, context, is_message=True)


async def show_purchase_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, is_message=False):
    """Show purchase confirmation with total price."""
    product_id = context.user_data.get('purchase_product_id')
    product_name = context.user_data.get('purchase_product_name')
    product_price = context.user_data.get('purchase_product_price')
    quantity = context.user_data.get('purchase_quantity')

    total = to_money(product_price * quantity)
    telegram_id = update.effective_user.id

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if not user:
            if is_message:
                await update.message.reply_text("❌ User not found.")
            else:
                await update.callback_query.edit_message_text("❌ User not found.")
            return ConversationHandler.END

        wallet_balance = user.wallet_balance
        lang = user.language
        has_sufficient_balance = wallet_balance >= total

        if has_sufficient_balance:
            balance_text = t('main_menu.wallet_balance', lang, balance=format_price(wallet_balance))
        else:
            balance_text = t('purchase.insufficient_balance', lang, balance=format_price(wallet_balance))

        title = t(
            'purchase.confirm_title', lang,
            product_name=product_name, price=format_price(product_price),
            quantity=quantity, total=format_price(total),
        )
        message = f"{title}\n\n{balance_text}"

        if has_sufficient_balance:
            keyboard = [
                [InlineKeyboardButton(t('purchase.button.confirm', lang), callback_data=f"confirm_purchase_{product_id}_{quantity}")],
                [InlineKeyboardButton(t('purchase.button.cancel', lang), callback_data="cancel_purchase")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton(t('purchase.button.topup_wallet', lang), callback_data="topup")],
                [InlineKeyboardButton(t('purchase.button.cancel', lang), callback_data="cancel_purchase")]
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        if is_message:
            await update.message.reply_text(message, reply_markup=reply_markup)
        else:
            query = update.callback_query
            if query.message.photo:
                await query.message.delete()
                await query.message.reply_text(message, reply_markup=reply_markup)
            else:
                await query.edit_message_text(message, reply_markup=reply_markup)

        return ConversationHandler.END


async def confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the confirmed purchase.

    Everything (stock check, key assignment, wallet debit, order creation) happens
    inside a single transaction. Previously the order was committed *before* the
    keys were assigned, so a key shortage raised ValueError and left behind a paid
    order with no keys, while the wallet was never debited.
    """
    query = update.callback_query
    await query.answer()

    if await check_user_banned_async(update.effective_user.id):
        await query.edit_message_text(t('purchase.banned', _get_user_lang(update.effective_user.id)))
        return

    # Extract product_id and quantity from callback data (format: confirm_purchase_123_5)
    try:
        parts = query.data.split("_")
        product_id = int(parts[2])
        quantity = int(parts[3])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid request.")
        return

    # Never trust a quantity that came back from the client.
    if quantity < 1 or quantity > MAX_PURCHASE_QUANTITY:
        await query.edit_message_text(f"❌ Quantity must be between 1 and {MAX_PURCHASE_QUANTITY}.")
        return

    telegram_id = update.effective_user.id
    result = None

    try:
        with get_db_session() as session:
            # SELECT ... FOR UPDATE where the backend supports it, so two rapid
            # taps on "Confirm Purchase" cannot both pass the balance check.
            user = session.query(User).filter_by(telegram_id=telegram_id).with_for_update().first()
            if not user:
                await query.edit_message_text("❌ User not found.")
                return

            product = session.query(Product).filter_by(id=product_id).with_for_update().first()
            if not product:
                await query.edit_message_text("❌ Product not found.")
                return

            if not product.is_active:
                await query.edit_message_text("❌ This product is no longer available.")
                return

            if product.stock_count < quantity:
                await query.edit_message_text(f"❌ Not enough stock. Only {product.stock_count} available.")
                return

            total = to_money(product.price * quantity)

            if user.wallet_balance < total:
                await query.edit_message_text(
                    f"❌ Insufficient balance.\n💰 Your balance: {format_price(user.wallet_balance)}\n💵 Required: {format_price(total)}"
                )
                return

            # Reserve the keys FIRST: if inventory does not actually back the
            # advertised stock_count, we bail out before charging anyone.
            assigned_keys = []
            if product.product_type == ProductType.KEY:
                # order_by keeps the lock order deterministic across
                # concurrent buyers on PostgreSQL, which avoids deadlocks.
                available_keys = session.query(ProductKey).filter_by(
                    product_id=product.id,
                    is_sold=False
                ).order_by(ProductKey.id).with_for_update().limit(quantity).all()

                if len(available_keys) < quantity:
                    # Re-sync the advertised stock with reality.
                    product.stock_count = len(available_keys)
                    session.commit()
                    await query.edit_message_text(
                        f"❌ Not enough keys in stock. Only {len(available_keys)} available."
                    )
                    return
                assigned_keys = available_keys

            order = Order(
                user_id=user.id,
                total_amount=total,
                status=OrderStatus.COMPLETED,
                completed_at=datetime.utcnow()
            )
            session.add(order)
            session.flush()  # populate order.id without ending the transaction

            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                price=product.price
            )

            if product.product_type == ProductType.KEY:
                key_values = []
                for key in assigned_keys:
                    key.is_sold = True
                    key.order_id = order.id
                    key.sold_at = datetime.utcnow()
                    key_values.append(key.key_value)
                order_item.delivered_asset = "\n".join(key_values)
                order_details = f"📦 {product.name} (x{quantity})\n🔐 Keys:\n{order_item.delivered_asset}\n"
            else:
                order_item.delivered_asset = product.download_link or ""
                order_details = f"📦 {product.name}\n🔗 Download: {order_item.delivered_asset}\n"

            product.stock_count -= quantity
            user.wallet_balance = to_money(user.wallet_balance - total)

            session.add(order_item)
            # commit happens on context-manager exit; a failure anywhere above
            # rolls the whole thing back.

            result = {
                'order_id': order.id,
                'total': total,
                'details': order_details,
                'lang': user.language,
            }
    except Exception:
        logger.exception("Purchase failed for user %s", telegram_id)
        await query.edit_message_text(
            "❌ Something went wrong while processing your purchase. "
            "Your wallet has not been charged. Please try again or contact support."
        )
        return

    if not result:
        return

    user_message = t(
        'purchase.success', result['lang'],
        total=format_price(result['total']), order_id=result['order_id'],
        details=result['details'],
    )

    keyboard = [
        [
            InlineKeyboardButton("🏠 Home", callback_data="main_menu"),
            InlineKeyboardButton("📋 Order History", callback_data="order_history")
        ]
    ]
    await query.edit_message_text(user_message, reply_markup=InlineKeyboardMarkup(keyboard))

    admin_message = f"""🛍 New Order Received

👤 User ID: {telegram_id}
💰 Amount: {format_price(result['total'])}
📝 Order ID: #{result['order_id']}

{result['details']}"""

    await notify_admin(context, admin_message)


async def cancel_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the purchase process."""
    query = update.callback_query
    await query.answer()

    from utils import create_main_menu_keyboard

    lang = _get_user_lang(update.effective_user.id)

    # Clear purchase data
    context.user_data.pop('purchase_product_id', None)
    context.user_data.pop('purchase_product_name', None)
    context.user_data.pop('purchase_product_price', None)
    context.user_data.pop('purchase_product_stock', None)
    context.user_data.pop('purchase_product_type', None)
    context.user_data.pop('purchase_quantity', None)

    await query.edit_message_text(
        t('purchase.cancelled', lang),
        reply_markup=create_main_menu_keyboard(lang)
    )

    return ConversationHandler.END


async def broadcast_availability_to_all_users(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to broadcast availability to all users every 12 hours (non-blocking with rate limiting)."""
    import asyncio
    from utils import build_availability_text

    logger.info("Starting availability broadcast to all users...")

    def _get_users_and_availability_sync():
        """Synchronous database operations run in thread pool."""
        try:
            with get_db_session() as session:
                from database import Category, Product

                # Get all non-banned users
                users = session.query(User).filter_by(is_banned=False).all()
                user_ids = [user.telegram_id for user in users]

                logger.info(f"Found {len(user_ids)} users to notify")

                # Build products by category dictionary
                products_by_category = {}
                categories = session.query(Category).all()

                for category in categories:
                    products = session.query(Product).filter_by(
                        category_id=category.id,
                        is_active=True
                    ).limit(15).all()

                    if products:
                        products_by_category[category.name] = products

                # Get availability text
                if not products_by_category:
                    availability_text = "📦 No products available yet."
                else:
                    availability_text = build_availability_text(products_by_category)

                return user_ids, availability_text
        except Exception as e:
            logger.error(f"Error in _get_users_and_availability_sync: {e}")
            raise

    try:
        # Run blocking database operations in thread pool
        user_ids, availability_text = await asyncio.to_thread(_get_users_and_availability_sync)
    except Exception as e:
        logger.error(f"Failed to get users and availability: {e}")
        return

    if not user_ids:
        logger.info("No users to notify, skipping broadcast")
        return  # No users to notify

    logger.info(f"Broadcasting availability to {len(user_ids)} users...")

    # Create availability keyboard
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton("🛒 Browse Products", callback_data="products")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send to all users with rate limiting
    success_count = 0
    fail_count = 0

    for telegram_id in user_ids:
        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=availability_text,
                reply_markup=reply_markup
            )
            success_count += 1

            # Rate limiting: 50ms delay = ~20 messages/second (well under Telegram's 30/sec limit)
            await asyncio.sleep(0.05)
        except Exception as e:
            # User may have blocked the bot
            logger.debug(f"Failed to send to {telegram_id}: {e}")
            fail_count += 1

    logger.info(f"Availability broadcast complete: {success_count} sent, {fail_count} failed")

    # Notify admin about broadcast completion
    try:
        from utils import notify_admin
        admin_message = f"""📢 Availability Broadcast Complete

✅ Sent successfully: {success_count}
❌ Failed: {fail_count}
👥 Total users: {len(user_ids)}"""

        await notify_admin(context, admin_message)
    except Exception as e:
        logger.error(f"Failed to notify admin: {e}")
