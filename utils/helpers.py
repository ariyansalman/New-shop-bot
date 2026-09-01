"""Helper utility functions for the Telegram bot."""

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import settings
from database import get_db_session, User
from .money import to_money

logger = logging.getLogger(__name__)

# In-memory cache for ban status (telegram_id: (is_banned, timestamp))
_ban_cache = {}
_ban_cache_lock = threading.Lock()
_BAN_CACHE_TTL = 30  # Cache ban status for 30 seconds
_BAN_CACHE_MAX = 10000  # Bound the cache so it cannot grow indefinitely


def is_admin(user_id: int) -> bool:
    """Check if a user is an admin based on Telegram ID.

    Multiple admins are supported via ADMIN_TELEGRAM_IDS (comma-separated,
    see config/settings.py); ADMIN_TELEGRAM_ID is always included too.
    """
    return user_id in settings.ADMIN_IDS


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


def format_price(price) -> str:
    """Format a money value (Decimal, float or int) to standard USD format."""
    return f"${to_money(price):.2f}"


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
    """Validate user input for payment amount. Returns (ok, Decimal amount, error)."""
    try:
        cleaned = (amount_str or "").strip().replace(",", "").replace("$", "")
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError, AttributeError):
        return False, 0, "Invalid amount. Please enter a valid number."

    # Decimal("nan") / Decimal("inf") parse without raising, just like float().
    if not amount.is_finite():
        return False, 0, "Invalid amount. Please enter a valid number."

    amount = to_money(amount)

    # settings.MIN/MAX_TOPUP_AMOUNT are plain floats (env-var parsed); Decimal
    # can't be compared against float directly, so normalize both sides.
    min_amount = to_money(settings.MIN_TOPUP_AMOUNT)
    max_amount = to_money(settings.MAX_TOPUP_AMOUNT)

    if amount < min_amount:
        return False, 0, f"Minimum amount is {min_amount:.2f} USD."
    if amount > max_amount:
        return False, 0, f"Amount is too large. Maximum is ${max_amount:,.2f}."
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
    except Exception:
        logger.exception("Error notifying admin")


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


async def check_user_banned_async(telegram_id: int) -> bool:
    """Async wrapper around check_user_banned.

    On a cache hit (the common case - see _BAN_CACHE_TTL) this returns
    immediately without touching a thread. On a cache miss it runs the
    blocking DB query in a worker thread instead of on the event loop, so
    one user's ban check can't stall every other user's handler while it
    waits on the database. Handlers should await this instead of calling
    check_user_banned directly.
    """
    with _ban_cache_lock:
        cached = _ban_cache.get(telegram_id)
    if cached:
        cached_value, cached_time = cached
        if (datetime.utcnow() - cached_time).total_seconds() < _BAN_CACHE_TTL:
            return cached_value

    return await asyncio.to_thread(check_user_banned, telegram_id)


def clear_ban_cache(telegram_id: int = None):
    """Clear ban cache for a specific user or all users (called when ban status changes)."""
    with _ban_cache_lock:
        if telegram_id is None:
            _ban_cache.clear()
        else:
            _ban_cache.pop(telegram_id, None)
