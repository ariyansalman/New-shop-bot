"""Helper utility functions for the Telegram bot."""

import math
import threading
from datetime import datetime, timedelta
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import settings
from database import get_db_session, User

# In-memory cache for ban status (telegram_id: (is_banned, timestamp))
_ban_cache = {}
_ban_cache_lock = threading.Lock()
_BAN_CACHE_TTL = 30  # Cache ban status for 30 seconds
_BAN_CACHE_MAX = 10000  # Bound the cache so it cannot grow indefinitely


def is_admin(user_id: int) -> bool:
    """Check if a user is an admin based on Telegram ID."""
    return user_id == settings.ADMIN_TELEGRAM_ID


def admin_only(func):
    """Decorator to restrict handler access to admin only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            # update.message is None for callback/edited updates.
            if update.message:
                await update.message.reply_text("⛔ You don't have permission to access this command.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Access denied.", show_alert=True)
            return
        return await func(update, context)
    return wrapper


def get_or_create_user(telegram_id: int, username: str = None) -> dict:
    """Get existing user or create a new one, returned as a plain dict.

    Returning the ORM object was unsafe: the session is closed by the context
    manager, so every later attribute access raised DetachedInstanceError.
    """
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()

        if not user:
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            session.commit()
            session.refresh(user)
        elif username and user.username != username:
            # Keep the cached username in sync when the person renames themselves.
            user.username = username
            session.commit()

        return {
            'id': user.id,
            'telegram_id': user.telegram_id,
            'username': user.username,
            'wallet_balance': user.wallet_balance,
            'is_banned': user.is_banned,
        }


def format_price(price: float) -> str:
    """Format price to standard USD format."""
    return f"${price:.2f}"


def format_datetime(dt: datetime) -> str:
    """Format datetime to readable string."""
    return dt.strftime("%b %d, %Y")


def calculate_expiry_time(hours: float = 1.0) -> datetime:
    """Calculate expiry datetime from now."""
    return datetime.utcnow() + timedelta(hours=hours)


def paginate_items(items, page: int, page_size: int = 5):
    """Paginate a list of items."""
    start = page * page_size
    end = start + page_size
    total_pages = (len(items) + page_size - 1) // page_size

    return {
        'items': items[start:end],
        'page': page,
        'total_pages': total_pages,
        'has_next': page < total_pages - 1,
        'has_prev': page > 0
    }


def validate_amount(amount_str: str) -> tuple:
    """Validate user input for payment amount."""
    try:
        cleaned = (amount_str or "").strip().replace(",", "").replace("$", "")
        amount = float(cleaned)
    except (ValueError, AttributeError):
        return False, 0, "Invalid amount. Please enter a valid number."

    # float() happily accepts "nan" and "inf".
    if not math.isfinite(amount):
        return False, 0, "Invalid amount. Please enter a valid number."

    amount = round(amount, 2)

    if amount < settings.MIN_TOPUP_AMOUNT:
        return False, 0, f"Minimum amount is {settings.MIN_TOPUP_AMOUNT:.2f} USD."
    if amount > settings.MAX_TOPUP_AMOUNT:
        return False, 0, f"Amount is too large. Maximum is ${settings.MAX_TOPUP_AMOUNT:,.2f}."
    return True, amount, ""


def format_product_display(product, include_description=False) -> str:
    """Format product information for display."""
    text = f"""📦 Name: {product.name}
💰 Price: {format_price(product.price)}
📦 In Stock: {product.stock_count}"""

    if include_description and product.description:
        text += f"\n📝 Description: {product.description}"

    return text


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send notification message to admin."""
    try:
        await context.bot.send_message(
            chat_id=settings.ADMIN_TELEGRAM_ID,
            text=message
        )
    except Exception as e:
        print(f"Error notifying admin: {e}")


def build_availability_text(products_by_category) -> str:
    """Build availability page text with products grouped by category."""
    text = "💬 Our available Products\n\n"

    for category_name, products in products_by_category.items():
        text += f"📦━━━━━{category_name}━━━━━📦\n"
        for product in products:
            text += f"{product.name} | {format_price(product.price)} | Available: {product.stock_count}\n"
        text += "\n"

    return text


def parse_keys_from_text(text: str) -> list:
    """Parse keys from text input (one key per line)."""
    keys = [line.strip() for line in text.split('\n') if line.strip()]
    return keys


def check_user_banned(telegram_id: int) -> bool:
    """Check if a user is banned (with caching for performance)."""
    with _ban_cache_lock:
        cached = _ban_cache.get(telegram_id)
    if cached:
        cached_value, cached_time = cached
        if (datetime.utcnow() - cached_time).total_seconds() < _BAN_CACHE_TTL:
            return cached_value

    # Cache miss or expired - query database
    with get_db_session() as session:
        # Use .scalar() for better performance - only fetch is_banned column
        is_banned = session.query(User.is_banned).filter_by(telegram_id=telegram_id).scalar()
        result = bool(is_banned) if is_banned is not None else False

    with _ban_cache_lock:
        if len(_ban_cache) >= _BAN_CACHE_MAX:
            _ban_cache.clear()
        _ban_cache[telegram_id] = (result, datetime.utcnow())

    return result


def clear_ban_cache(telegram_id: int = None):
    """Clear ban cache for a specific user or all users (called when ban status changes)."""
    with _ban_cache_lock:
        if telegram_id is None:
            _ban_cache.clear()
        else:
            _ban_cache.pop(telegram_id, None)
