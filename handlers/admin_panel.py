"""The Admin Panel's navigation: one main menu, sixteen sections.

This module owns the shape of the panel, not the work behind it. Every
button either points at a handler that already existed - admin_products,
admin_orders, payadmin_menu, admin_action_log and the rest keep their
callbacks untouched - or at a screen defined here that reads existing
data. Nothing that already worked was reimplemented.

Sections are declared as data rather than written as sixteen near
identical handlers, so the layout rule ("two columns, Back full width
underneath") is applied in one place and a new section is three lines
rather than a new file.

Where the requested panel names a capability this codebase does not have -
a coupon engine, providers with no integration - the screen says so
plainly instead of showing a button that leads nowhere or a status that is
not true. See UNAVAILABLE below.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database import (
    get_db_session, User, Order, OrderItem, Product, Transaction,
    AdminActionLog, OrderStatus, TransactionStatus, PaymentMethod,
    UNLIMITED_STOCK,
)
from services import payment_methods
from utils import is_admin, format_price, two_column_rows
from utils.money import to_money

logger = logging.getLogger(__name__)

# Products at or below this are flagged on the dashboard. Matches the
# storefront's own "Only N left" threshold so the two agree.
LOW_STOCK = 5

# How many rows a listing screen shows before paging.
PAGE_SIZE = 8


# ======================================================================
# Navigation
# ======================================================================

@dataclass(frozen=True)
class Section:
    key: str                      # callback is apanel_<key>
    title: str                    # heading on its own screen
    label: str                    # button on the main menu
    buttons: tuple = field(default=())   # (label, callback_data) pairs
    body: object = None           # optional async builder for the text


def _screen(title: str, lines=(), buttons=(), back="admin_menu",
            extra_rows=()) -> tuple:
    """Every panel screen has the same shape: heading, rule, body, grid, Back."""
    text = [f"{title}", "━━━━━━━━━━━━━━━━━━━━"]
    if lines:
        text += ["", *lines]
    text += ["", "━━━━━━━━━━━━━━━━━━━━"]

    keyboard = two_column_rows([InlineKeyboardButton(label, callback_data=data)
                                for label, data in buttons])
    keyboard += [list(row) for row in extra_rows]
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data=back)])
    return "\n".join(text), InlineKeyboardMarkup(keyboard)


async def _deny(update: Update) -> bool:
    """Refuse a non-admin, before answering the callback.

    Telegram accepts one answer per callback and drops the rest, so this
    has to answer first or the alert never appears.
    """
    if is_admin(update.effective_user.id):
        return False
    await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    return True


# ======================================================================
# Figures - one query pass, off the event loop
# ======================================================================

def _dashboard_sync() -> dict:
    """Everything the dashboard shows, in one session."""
    since = datetime.utcnow() - timedelta(days=7)

    with get_db_session() as session:
        revenue = (session.query(func.coalesce(func.sum(Order.total_amount), 0))
                   .filter(Order.status == OrderStatus.COMPLETED).scalar())

        def orders(status):
            return session.query(func.count(Order.id)).filter(
                Order.status == status).scalar()

        def payments(status):
            return session.query(func.count(Transaction.id)).filter(
                Transaction.status == status).scalar()

        return {
            'users': session.query(func.count(User.id)).scalar(),
            'new_users': session.query(func.count(User.id)).filter(
                User.created_at >= since).scalar(),
            'orders': session.query(func.count(Order.id)).scalar(),
            'orders_processing': orders(OrderStatus.PROCESSING),
            'orders_completed': orders(OrderStatus.COMPLETED),
            'revenue': to_money(revenue),
            'payments_pending': (payments(TransactionStatus.PENDING)
                                 + payments(TransactionStatus.VERIFYING)),
            'payments_review': payments(TransactionStatus.MANUAL_REVIEW),
            'payments_failed': payments(TransactionStatus.FAILED),
            'products_active': session.query(func.count(Product.id)).filter(
                Product.is_active.is_(True)).scalar(),
            'products_low': session.query(func.count(Product.id)).filter(
                Product.is_active.is_(True),
                Product.stock_count > 0,
                Product.stock_count <= LOW_STOCK).scalar(),
            'products_out': session.query(func.count(Product.id)).filter(
                Product.is_active.is_(True), Product.stock_count <= 0).scalar(),
        }


def _database_ok() -> bool:
    from database.db import check_connection
    try:
        return check_connection()
    except Exception:
        return False


# Providers the requested panel names that this codebase has no code for.
# Listed rather than silently dropped, and never shown as "connected":
# a green light for an integration that does not exist is worse than an
# empty panel.
UNAVAILABLE = (
    ("🔵 Bybit", "no integration in this build"),
    ("🩷 ZiniPay", "no integration in this build"),
    ("🩷 bKash", "no integration in this build"),
    ("🟠 Nagad", "no integration in this build"),
)


def _integration_rows() -> list:
    """Live status for what is actually wired up."""
    rows = [("🤖 Telegram", "🟢 connected" if settings.BOT_TOKEN else "🔴 no token"),
            ("🗄️ Database", "🟢 connected" if _database_ok() else "🔴 unreachable")]

    for spec in payment_methods.SPECS:
        if not spec.configured():
            rows.append((spec.label, "⚪️ not configured"))
        elif payment_methods.available(spec.key):
            rows.append((spec.label, "🟢 live"))
        else:
            rows.append((spec.label, "🟠 switched off"))

    rows += [(name, f"⚫️ {why}") for name, why in UNAVAILABLE]
    return rows


async def _dashboard_body() -> list:
    figures = await asyncio.to_thread(_dashboard_sync)
    integrations = await asyncio.to_thread(_integration_rows)

    lines = [
        "👥 USERS",
        f"   Total: {figures['users']}   ·   New (7d): {figures['new_users']}",
        "",
        "🛒 ORDERS",
        f"   Total: {figures['orders']}",
        f"   Processing: {figures['orders_processing']}   ·   "
        f"Completed: {figures['orders_completed']}",
        "",
        "💰 REVENUE",
        f"   Completed orders: {format_price(figures['revenue'])}",
        "",
        "💳 PAYMENTS",
        f"   In progress: {figures['payments_pending']}",
        f"   Manual review: {figures['payments_review']}   ·   "
        f"Failed: {figures['payments_failed']}",
        "",
        "📦 PRODUCTS",
        f"   Active: {figures['products_active']}",
        f"   Low stock: {figures['products_low']}   ·   "
        f"Sold out: {figures['products_out']}",
        "",
        "🔌 STATUS",
    ]
    lines += [f"   {name} · {state}" for name, state in integrations]
    return lines


async def _integrations_body() -> list:
    rows = await asyncio.to_thread(_integration_rows)
    return ([f"{name}   {state}" for name, state in rows]
            + ["", "Credentials live in the server environment and are",
               "never shown here. Open a payment method for its own",
               "status and connectivity test."])


def _reports_sync(days: int) -> dict:
    """Figures for a window, used by every report length."""
    since = datetime.utcnow() - timedelta(days=days)

    with get_db_session() as session:
        revenue = (session.query(func.coalesce(func.sum(Order.total_amount), 0))
                   .filter(Order.status == OrderStatus.COMPLETED,
                           Order.created_at >= since).scalar())
        orders = (session.query(func.count(Order.id))
                  .filter(Order.created_at >= since).scalar())
        users = (session.query(func.count(User.id))
                 .filter(User.created_at >= since).scalar())
        topups = (session.query(func.coalesce(func.sum(Transaction.amount), 0))
                  .filter(Transaction.status == TransactionStatus.COMPLETED,
                          Transaction.created_at >= since).scalar())

        # Best sellers by units, which is what restocking decisions need.
        best = (session.query(Product.name, func.sum(OrderItem.quantity))
                .join(OrderItem, OrderItem.product_id == Product.id)
                .join(Order, Order.id == OrderItem.order_id)
                .filter(Order.status == OrderStatus.COMPLETED,
                        Order.created_at >= since)
                .group_by(Product.name)
                .order_by(func.sum(OrderItem.quantity).desc())
                .limit(5).all())

        by_method = (session.query(Transaction.payment_method,
                                   func.count(Transaction.id))
                     .filter(Transaction.status == TransactionStatus.COMPLETED,
                             Transaction.created_at >= since)
                     .group_by(Transaction.payment_method).all())

        referred = (session.query(func.count(User.id))
                    .filter(User.referred_by_id.isnot(None),
                            User.created_at >= since).scalar())

        return {
            'revenue': to_money(revenue), 'orders': orders, 'users': users,
            'topups': to_money(topups), 'best': list(best),
            'by_method': list(by_method), 'referred': referred,
        }


def _report_body(days: int, title: str):
    async def build():
        data = await asyncio.to_thread(_reports_sync, days)
        lines = [
            f"📅 {title}", "",
            f"💰 Revenue: {format_price(data['revenue'])}",
            f"🛒 Orders: {data['orders']}",
            f"💳 Top-ups credited: {format_price(data['topups'])}",
            f"👥 New users: {data['users']}   ·   referred: {data['referred']}",
        ]
        if data['by_method']:
            lines += ["", "💳 Completed top-ups by method"]
            lines += [f"   {_METHOD_LABEL.get(m, str(m))}: {n}"
                      for m, n in data['by_method']]
        lines += ["", "📦 Best sellers"]
        if data['best']:
            lines += [f"   {name} · {int(units)} sold" for name, units in data['best']]
        else:
            lines.append("   Nothing sold in this window.")
        return lines
    return build


_METHOD_LABEL = {
    PaymentMethod.CRYPTO_WALLET: "CryptoBot",
    PaymentMethod.CARD: "Card",
    PaymentMethod.BINANCE_PAY: "Binance Pay",
}


def _system_sync() -> dict:
    with get_db_session() as session:
        recent_admin = (session.query(func.count(AdminActionLog.id))
                        .filter(AdminActionLog.created_at
                                >= datetime.utcnow() - timedelta(days=1)).scalar())
        stuck = (session.query(func.count(Transaction.id))
                 .filter(Transaction.status == TransactionStatus.MANUAL_REVIEW)
                 .scalar())
    return {'admin_actions_24h': recent_admin, 'stuck_payments': stuck}


async def _system_body() -> list:
    data = await asyncio.to_thread(_system_sync)
    db_ok = await asyncio.to_thread(_database_ok)

    return [
        f"🤖 Bot: 🟢 running   ·   token {'set' if settings.BOT_TOKEN else 'MISSING'}",
        f"🗄️ Database: {'🟢 reachable' if db_ok else '🔴 unreachable'}",
        f"🌐 HTTP server: {'🟢 enabled' if settings.WEBHOOK_ENABLED else '⚪️ disabled'}"
        f" (port {settings.PORT})",
        "",
        "⚙️ BACKGROUND JOBS",
        f"   Payment poll: every {settings.PAYMENT_CHECK_INTERVAL}s",
        f"   Binance retry: every {settings.BINANCE_VERIFY_RETRY_INTERVAL}s",
        "",
        "⚠️ NEEDS ATTENTION",
        f"   Payments in manual review: {data['stuck_payments']}",
        "",
        f"📜 Admin actions (24h): {data['admin_actions_24h']}",
        "",
        "💾 Backups are the database provider's own (Supabase);",
        "   this bot does not take or restore them.",
    ]


async def _security_body() -> list:
    """Who can administer the bot. Never what their credentials are."""
    admins = sorted(settings.ADMIN_IDS)
    lines = [f"👑 ADMINS ({len(admins)})"]
    lines += [f"   {admin_id}"
              + ("   ← primary" if admin_id == settings.ADMIN_TELEGRAM_ID else "")
              for admin_id in admins]
    lines += [
        "", "🔑 HOW ACCESS IS GRANTED",
        "   ADMIN_TELEGRAM_ID plus ADMIN_TELEGRAM_IDS in the",
        "   server environment. Every admin has the same rights;",
        "   there are no per-admin permissions in this build.",
        "", "🔐 SECRETS",
        "   API keys, tokens and the database password live in the",
        "   environment only. No screen in this panel can show or",
        "   change them - see the payment screens for masked status.",
        "", "📋 Admin actions are recorded in Activity Logs.",
    ]
    return lines


async def _unavailable_body(name: str, needs: tuple):
    async def build():
        return [f"{name} is not built in this deployment.", "",
                "It would need:"] + [f"   • {item}" for item in needs] + [
                "", "Nothing here is hidden behind this button - the",
                "feature genuinely does not exist yet, and showing a",
                "menu that cannot act would be worse than saying so."]
    return build


# ======================================================================
# The sections
# ======================================================================
#
# Callbacks that already existed are reused verbatim, so every flow the
# panel used to reach still works and nothing is implemented twice.

SECTIONS = {}


def _section(key, title, label, buttons=(), body=None):
    SECTIONS[key] = Section(key=key, title=title, label=label,
                            buttons=tuple(buttons), body=body)


_section("dashboard", "📊 DASHBOARD", "📊 Dashboard", [
    ("🛒 Orders", "apanel_orders"), ("💳 Payments", "payadmin_menu"),
    ("👥 Users", "admin_users"), ("📦 Products", "admin_products"),
    ("📊 Reports", "apanel_reports"), ("🔄 Refresh", "apanel_dashboard"),
], body=_dashboard_body)

_section("orders", "🛒 ORDER MANAGEMENT", "🛒 Orders", [
    ("📋 All Orders", "admin_view_orders"), ("🟡 Pending", "apanel_ord_pending"),
    ("🟢 Completed", "apanel_ord_completed"), ("🚫 Cancelled", "apanel_ord_cancelled"),
    ("🚨 Disputes", "admin_view_disputes"), ("🔎 Search Order", "apanel_ord_search"),
    ("✅ Confirm Order", "admin_confirm_order"), ("❌ Cancel Order", "admin_cancel_order"),
])

_section("products", "📦 PRODUCT MANAGEMENT", "📦 Products", [
    ("➕ Add Product", "admin_create_product"), ("📋 All Products", "apanel_prod_all"),
    ("✏️ Edit Product", "admin_edit_product"), ("📂 Categories", "admin_manage_categories"),
    ("📦 Restock Keys", "admin_restock_keys"), ("🟡 Low Stock", "apanel_prod_low"),
    ("🟢 Active", "apanel_prod_active"), ("🔴 Disabled", "apanel_prod_disabled"),
    ("📊 Sales Stats", "apanel_reports"),
])

_section("users", "👥 USER MANAGEMENT", "👥 Users", [
    ("👥 All Users", "admin_view_users"), ("🔎 Search User", "apanel_usr_search"),
    ("🚫 Banned Users", "apanel_usr_banned"), ("💰 Top Balances", "apanel_usr_balances"),
    ("🎁 Referral Stats", "apanel_referrals"), ("📊 User Stats", "apanel_dashboard"),
])

_section("wallet", "💰 WALLET MANAGEMENT", "💰 Wallet", [
    ("➕ Add Balance", "apanel_wal_add"), ("➖ Deduct Balance", "apanel_wal_deduct"),
    ("🔎 User Balance", "apanel_usr_search"), ("📋 Transactions", "payadmin_menu"),
    ("📊 Wallet Stats", "apanel_wal_stats"),
])

_section("referrals", "🎁 REFERRAL MANAGEMENT", "🎁 Referrals", [
    ("💰 Reward Settings", "admin_referral"), ("📊 Statistics", "apanel_ref_stats"),
    ("🏆 Top Referrers", "apanel_ref_top"), ("📋 Referral History", "apanel_ref_history"),
])

_section("broadcast", "📢 BROADCAST", "📢 Broadcast", [
    ("💬 Text Only", "admin_broadcast_text"), ("🖼 Image + Text", "admin_broadcast_image"),
    ("📊 Audience", "apanel_bc_audience"),
])

_section("reports", "📊 REPORTS", "📊 Reports", [
    ("📅 Daily", "apanel_rep_daily"), ("📆 Weekly", "apanel_rep_weekly"),
    ("🗓️ Monthly", "apanel_rep_monthly"),
])

_section("integrations", "🔌 INTEGRATIONS", "🔌 Integrations", [
    ("💳 Payment Methods", "payadmin_menu"), ("🟡 Binance Pay", "binadmin_menu"),
    ("🛠️ System", "apanel_system"), ("🔄 Refresh", "apanel_integrations"),
], body=_integrations_body)

_section("settings", "⚙️ SETTINGS", "⚙️ Settings", [
    ("🛠️ Store Settings", "admin_settings"), ("💳 Payment Settings", "payadmin_menu"),
    ("🎁 Referral Settings", "admin_referral"), ("📜 Terms", "admin_terms"),
    ("❓ FAQ", "admin_faq"),
])

_section("security", "🔐 SECURITY", "🔐 Security", [
    ("📜 Activity Logs", "admin_action_log"), ("🔌 Integrations", "apanel_integrations"),
], body=_security_body)

_section("logs", "📜 ACTIVITY LOGS", "📜 Activity Logs", [
    ("👨‍💼 Admin Activity", "admin_action_log"), ("💳 Payments", "payadmin_menu"),
    ("🛒 Orders", "admin_view_orders"), ("🛠️ System", "apanel_system"),
])

_section("system", "🛠️ SYSTEM", "🛠️ System", [
    ("🔄 Refresh Status", "apanel_system"), ("🔌 Integrations", "apanel_integrations"),
    ("📜 Activity Logs", "admin_action_log"),
], body=_system_body)

# The two the requested panel names that this codebase has no code for.
_section("coupons", "🎟️ COUPONS", "🎟️ Coupons")
_section("notifications", "🔔 NOTIFICATIONS", "🔔 Notifications")

# Order of the main menu grid, two per row.
MENU_ORDER = (
    "dashboard", "orders", "products", "users", "payments", "wallet",
    "referrals", "coupons", "broadcast", "notifications", "reports",
    "integrations", "settings", "security", "logs", "system",
)

# Payments has no section screen of its own: payadmin_menu already is one.
MENU_TARGET = {"payments": "payadmin_menu"}
MENU_LABEL = {"payments": "💳 Payments"}


_UNAVAILABLE_SECTIONS = {
    "coupons": ("A coupon engine",
                ("a coupons table (code, discount, limits, expiry)",
                 "redemption checks inside the purchase flow",
                 "per-code usage accounting")),
    "notifications": ("Configurable notification categories",
                      ("somewhere to store the per-category switches",
                       "the notify calls reading them before sending")),
}


# ======================================================================
# Screens
# ======================================================================

def admin_panel_main_markup():
    """The main menu keyboard, so /admin and the button share one layout."""
    buttons = [(MENU_LABEL[key] if key in MENU_LABEL else SECTIONS[key].label,
                MENU_TARGET.get(key, f"apanel_{key}"))
               for key in MENU_ORDER]

    keyboard = two_column_rows([InlineKeyboardButton(label, callback_data=data)
                                for label, data in buttons])
    keyboard.append([InlineKeyboardButton("◀️ Back to Main Menu",
                                          callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


async def admin_panel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The Admin Panel main menu."""
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    await _render(query, "👑 ADMIN PANEL\n━━━━━━━━━━━━━━━━━━━━",
                  admin_panel_main_markup())


async def admin_panel_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any section screen (callback_data: apanel_<key>)."""
    query = update.callback_query
    if await _deny(update):
        return

    key = query.data[len("apanel_"):]
    section = SECTIONS.get(key)
    if section is None:
        await query.answer("Unknown section.", show_alert=True)
        return
    await query.answer()

    if key in _UNAVAILABLE_SECTIONS:
        name, needs = _UNAVAILABLE_SECTIONS[key]
        lines = [f"{name} is not built in this deployment.", "", "It would need:"]
        lines += [f"   • {item}" for item in needs]
        lines += ["", "Showing a menu that cannot act would be worse",
                  "than saying so."]
    else:
        lines = await section.body() if section.body else []

    text, markup = _screen(section.title, lines, section.buttons)
    await _render(query, text, markup)


_REPORT_WINDOWS = {
    "daily": (1, "TODAY"),
    "weekly": (7, "LAST 7 DAYS"),
    "monthly": (30, "LAST 30 DAYS"),
}


async def admin_panel_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One report window (apanel_rep_daily | weekly | monthly)."""
    query = update.callback_query
    if await _deny(update):
        return

    key = query.data[len("apanel_rep_"):]
    window = _REPORT_WINDOWS.get(key)
    if window is None:
        await query.answer("Unknown report.", show_alert=True)
        return
    await query.answer()

    days, title = window
    lines = await _report_body(days, title)()

    buttons = [(f"📅 {label.title()}", f"apanel_rep_{other}")
               for other, (_d, label) in _REPORT_WINDOWS.items() if other != key]
    text, markup = _screen("📊 REPORT", lines, buttons, back="apanel_reports")
    await _render(query, text, markup)


async def _render(query, text, markup):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        # Usually "message is not modified" - the same screen is already up,
        # which Refresh hits whenever nothing has changed.
        logger.debug("Admin panel screen not re-rendered", exc_info=True)


# ======================================================================
# Filtered listings
# ======================================================================

def _order_rows_sync(status) -> list:
    with get_db_session() as session:
        rows = (session.query(Order).filter(Order.status == status)
                .order_by(Order.created_at.desc()).limit(PAGE_SIZE).all())
        total = session.query(func.count(Order.id)).filter(
            Order.status == status).scalar()
        return total, [(o.id, o.user.telegram_id if o.user else None,
                        o.total_amount, o.created_at) for o in rows]


def _product_rows_sync(kind) -> tuple:
    with get_db_session() as session:
        q = session.query(Product)
        if kind == "active":
            q = q.filter(Product.is_active.is_(True))
        elif kind == "disabled":
            q = q.filter(Product.is_active.is_(False))
        elif kind == "low":
            q = q.filter(Product.is_active.is_(True),
                         Product.stock_count <= LOW_STOCK)
        total = q.count()
        rows = q.order_by(Product.name).limit(PAGE_SIZE).all()
        return total, [(p.id, p.name, p.price, p.stock_count) for p in rows]


def _user_rows_sync(kind) -> tuple:
    with get_db_session() as session:
        q = session.query(User)
        if kind == "banned":
            q = q.filter(User.is_banned.is_(True))
            q = q.order_by(User.id.desc())
        else:
            q = q.order_by(User.wallet_balance.desc())
        total = q.count()
        rows = q.limit(PAGE_SIZE).all()
        return total, [(u.telegram_id, u.username, u.wallet_balance,
                        u.is_banned) for u in rows]


_ORDER_FILTERS = {
    "pending": ("🟡 PENDING ORDERS", OrderStatus.PROCESSING),
    "completed": ("🟢 COMPLETED ORDERS", OrderStatus.COMPLETED),
    "cancelled": ("🚫 CANCELLED ORDERS", OrderStatus.CANCELLED),
}


async def admin_panel_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A filtered listing (apanel_ord_* / apanel_prod_* / apanel_usr_*).

    Rows stay one per line: names, prices and usernames are of
    unpredictable length, and two records side by side on a phone make a
    mis-tap easy.
    """
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    data = query.data
    lines, title, back = [], "", "admin_menu"

    if data.startswith("apanel_ord_"):
        back = "apanel_orders"
        key = data[len("apanel_ord_"):]
        if key == "search":
            title = "🔎 SEARCH ORDER"
            lines = ["Open 📋 All Orders and pick from the list, or use",
                     "✅ Confirm Order / ❌ Cancel Order, which both ask",
                     "for an order id.",
                     "", "There is no separate search index for orders in",
                     "this build."]
        else:
            title, status = _ORDER_FILTERS[key]
            total, rows = await asyncio.to_thread(_order_rows_sync, status)
            lines = _order_lines(total, rows)

    elif data.startswith("apanel_prod_"):
        back = "apanel_products"
        kind = data[len("apanel_prod_"):]
        title = {"all": "📋 ALL PRODUCTS", "active": "🟢 ACTIVE PRODUCTS",
                 "disabled": "🔴 DISABLED PRODUCTS",
                 "low": "🟡 LOW STOCK"}[kind]
        total, rows = await asyncio.to_thread(_product_rows_sync, kind)
        lines = _product_lines(total, rows)

    elif data.startswith("apanel_usr_"):
        back = "apanel_users"
        kind = data[len("apanel_usr_"):]
        if kind == "search":
            title = "🔎 SEARCH USER"
            lines = ["Open 👥 All Users and pick from the list - each",
                     "entry opens that user's balance, orders and the",
                     "ban control.",
                     "", "There is no lookup by id or username in this",
                     "build."]
        else:
            title = ("🚫 BANNED USERS" if kind == "banned"
                     else "💰 TOP BALANCES")
            total, rows = await asyncio.to_thread(_user_rows_sync, kind)
            lines = _user_lines(total, rows)

    text, markup = _screen(title, lines, (), back=back)
    await _render(query, text, markup)


def _order_lines(total, rows) -> list:
    if not rows:
        return ["No orders in this state."]
    lines = [f"Showing {len(rows)} of {total}", ""]
    for order_id, telegram_id, amount, created in rows:
        when = created.strftime("%Y-%m-%d %H:%M") if created else "-"
        lines.append(f"🆔 #{order_id} · {format_price(amount)}")
        lines.append(f"   👤 {telegram_id} · {when}")
    return lines


def _product_lines(total, rows) -> list:
    if not rows:
        return ["Nothing here."]
    lines = [f"Showing {len(rows)} of {total}", ""]
    for _pid, name, price, stock in rows:
        left = "unlimited" if (stock or 0) >= UNLIMITED_STOCK else str(stock or 0)
        lines.append(f"📦 {name}")
        lines.append(f"   {format_price(price)} · stock {left}")
    return lines


def _user_lines(total, rows) -> list:
    if not rows:
        return ["Nobody here."]
    lines = [f"Showing {len(rows)} of {total}", ""]
    for telegram_id, username, balance, banned in rows:
        handle = f"@{username}" if username else "—"
        flag = " 🚫" if banned else ""
        lines.append(f"👤 {telegram_id} · {handle}{flag}")
        lines.append(f"   {format_price(balance)}")
    return lines


# ======================================================================
# Referral, wallet and broadcast figures
# ======================================================================

def _referral_stats_sync() -> dict:
    with get_db_session() as session:
        referred = session.query(func.count(User.id)).filter(
            User.referred_by_id.isnot(None)).scalar()
        rewarded = session.query(func.count(User.id)).filter(
            User.referral_rewarded_at.isnot(None)).scalar()
        paid = session.query(func.coalesce(
            func.sum(User.referral_earnings), 0)).scalar()
        referrers = (session.query(func.count(func.distinct(User.referred_by_id)))
                     .filter(User.referred_by_id.isnot(None)).scalar())
        return {'referred': referred, 'rewarded': rewarded,
                'paid': to_money(paid), 'referrers': referrers}


def _top_referrers_sync() -> list:
    with get_db_session() as session:
        referrer = User.__table__.alias("referrer")
        rows = (session.query(referrer.c.telegram_id, referrer.c.username,
                              referrer.c.referral_earnings,
                              func.count(User.id))
                .join(referrer, User.referred_by_id == referrer.c.id)
                .group_by(referrer.c.telegram_id, referrer.c.username,
                          referrer.c.referral_earnings)
                .order_by(func.count(User.id).desc())
                .limit(10).all())
        return list(rows)


def _referral_history_sync() -> list:
    with get_db_session() as session:
        referrer = User.__table__.alias("referrer")
        rows = (session.query(User.telegram_id, referrer.c.telegram_id,
                              User.referral_rewarded_at, User.created_at)
                .join(referrer, User.referred_by_id == referrer.c.id)
                .order_by(User.created_at.desc())
                .limit(PAGE_SIZE).all())
        return list(rows)


def _wallet_stats_sync() -> dict:
    with get_db_session() as session:
        held = session.query(func.coalesce(func.sum(User.wallet_balance), 0)).scalar()
        credited = (session.query(func.coalesce(func.sum(Transaction.amount), 0))
                    .filter(Transaction.status == TransactionStatus.COMPLETED)
                    .scalar())
        spent = (session.query(func.coalesce(func.sum(Order.total_amount), 0))
                 .filter(Order.status == OrderStatus.COMPLETED).scalar())
        with_balance = session.query(func.count(User.id)).filter(
            User.wallet_balance > 0).scalar()
        return {'held': to_money(held), 'credited': to_money(credited),
                'spent': to_money(spent), 'with_balance': with_balance}


def _audience_sync() -> dict:
    with get_db_session() as session:
        total = session.query(func.count(User.id)).scalar()
        banned = session.query(func.count(User.id)).filter(
            User.is_banned.is_(True)).scalar()
        return {'total': total, 'banned': banned, 'reachable': total - banned}


async def admin_panel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The read-only figure screens hung off Referrals, Wallet and Broadcast."""
    query = update.callback_query
    if await _deny(update):
        return
    await query.answer()

    data = query.data
    if data == "apanel_ref_stats":
        s = await asyncio.to_thread(_referral_stats_sync)
        title, back = "📊 REFERRAL STATISTICS", "apanel_referrals"
        lines = [f"👥 Users who arrived via a link: {s['referred']}",
                 f"🏆 Referrers with at least one: {s['referrers']}",
                 f"✅ Referrals that bought: {s['rewarded']}",
                 f"💰 Total paid out: {format_price(s['paid'])}",
                 "",
                 "A referral pays out only once the invited user",
                 "completes their first purchase, so 'referred' and",
                 "'bought' are meant to differ."]

    elif data == "apanel_ref_top":
        rows = await asyncio.to_thread(_top_referrers_sync)
        title, back = "🏆 TOP REFERRERS", "apanel_referrals"
        if rows:
            lines = []
            for place, (telegram_id, username, earnings, count) in enumerate(rows, 1):
                handle = f"@{username}" if username else "—"
                lines.append(f"{place}. 👤 {telegram_id} · {handle}")
                lines.append(f"    {count} invited · earned "
                             f"{format_price(earnings or 0)}")
        else:
            lines = ["Nobody has invited anyone yet."]

    elif data == "apanel_ref_history":
        rows = await asyncio.to_thread(_referral_history_sync)
        title, back = "📋 REFERRAL HISTORY", "apanel_referrals"
        if rows:
            lines = []
            for invited, referrer, rewarded_at, joined in rows:
                when = joined.strftime("%Y-%m-%d") if joined else "-"
                state = "✅ paid" if rewarded_at else "⏳ not yet"
                lines.append(f"👤 {invited} ← {referrer}")
                lines.append(f"   {when} · {state}")
        else:
            lines = ["No referrals recorded yet."]

    elif data == "apanel_wal_stats":
        s = await asyncio.to_thread(_wallet_stats_sync)
        title, back = "📊 WALLET STATISTICS", "apanel_wallet"
        lines = [f"💰 Balance held by users: {format_price(s['held'])}",
                 f"👥 Users with a balance: {s['with_balance']}",
                 "",
                 f"⬆️ Credited by top-ups: {format_price(s['credited'])}",
                 f"⬇️ Spent on orders: {format_price(s['spent'])}",
                 "",
                 "Held is a liability: it is money customers have paid",
                 "you and can still spend."]

    else:  # apanel_bc_audience
        s = await asyncio.to_thread(_audience_sync)
        title, back = "👥 BROADCAST AUDIENCE", "apanel_broadcast"
        lines = [f"Total users: {s['total']}",
                 f"🚫 Banned (skipped): {s['banned']}",
                 f"📨 Would receive: {s['reachable']}",
                 "",
                 "A broadcast goes to everyone who has started the bot",
                 "and is not banned. There is no audience segmentation",
                 "in this build."]

    text, markup = _screen(title, lines, (), back=back)
    await _render(query, text, markup)


# ======================================================================
# Manual balance adjustment
# ======================================================================
#
# The only screen in the panel that moves money. It is deliberately two
# steps with a confirmation in between, and every adjustment writes an
# AdminActionLog row saying who did it, to whom, and for how much.

WALLET_TARGET, WALLET_AMOUNT, WALLET_CONFIRM = range(40, 43)


def _adjust_balance_sync(telegram_id: int, delta, admin_telegram_id: int):
    """Apply an adjustment under a row lock. Returns (old, new) or None."""
    from utils.audit import log_admin_action

    with get_db_session() as session:
        user = (session.query(User).filter_by(telegram_id=telegram_id)
                .with_for_update().first())
        if user is None:
            return None

        old = to_money(user.wallet_balance)
        new = to_money(old + delta)
        if new < 0:
            # Refuse rather than silently clamping: an admin deducting more
            # than someone has has miscounted, and a wallet that quietly
            # floors at zero hides that.
            return "insufficient", old

        user.wallet_balance = new
        log_admin_action(
            session, admin_telegram_id,
            "wallet_credit" if delta > 0 else "wallet_debit",
            target_type="user", target_id=user.id,
            details=f"telegram_id={telegram_id}, {old} -> {new}",
        )
        session.commit()
        return old, new


async def wallet_adjust_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask which user (apanel_wal_add | apanel_wal_deduct)."""
    from telegram.ext import ConversationHandler

    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    adding = query.data == "apanel_wal_add"
    context.user_data['wallet_adding'] = adding

    await query.edit_message_text(
        f"{'➕ ADD BALANCE' if adding else '➖ DEDUCT BALANCE'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the customer's Telegram ID.\n"
        "You can find it in 👥 Users.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Cancel", callback_data="apanel_wallet")]]))
    return WALLET_TARGET


async def wallet_adjust_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Take the Telegram id and show who it is before asking for an amount."""
    raw = (update.message.text or "").strip()
    cancel = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Cancel", callback_data="apanel_wallet")]])

    try:
        telegram_id = int(raw)
    except ValueError:
        await update.message.reply_text(
            "❌ That is not a Telegram ID. Send digits only:",
            reply_markup=cancel)
        return WALLET_TARGET

    def _sync():
        with get_db_session() as session:
            user = session.query(User).filter_by(telegram_id=telegram_id).first()
            if user is None:
                return None
            return user.username, to_money(user.wallet_balance)

    found = await asyncio.to_thread(_sync)
    if found is None:
        await update.message.reply_text(
            "❌ No user with that Telegram ID has started the bot.",
            reply_markup=cancel)
        return WALLET_TARGET

    username, balance = found
    context.user_data['wallet_target'] = telegram_id

    adding = context.user_data.get('wallet_adding', True)
    await update.message.reply_text(
        f"👤 {telegram_id} · {'@' + username if username else '—'}\n"
        f"💰 Current balance: {format_price(balance)}\n\n"
        f"How much to {'add' if adding else 'deduct'}? (for example 5.00)",
        reply_markup=cancel)
    return WALLET_AMOUNT


async def wallet_adjust_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Take the amount and ask for confirmation before touching anything."""
    from utils import money_or_none

    amount = money_or_none(update.message.text)
    cancel = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Cancel", callback_data="apanel_wallet")]])

    if amount is None or amount <= 0:
        await update.message.reply_text(
            "❌ Send a positive number, for example 5.00:", reply_markup=cancel)
        return WALLET_AMOUNT

    context.user_data['wallet_amount'] = amount
    adding = context.user_data.get('wallet_adding', True)
    telegram_id = context.user_data.get('wallet_target')

    await update.message.reply_text(
        "⚠️ CONFIRM\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{'➕ Add' if adding else '➖ Deduct'} {format_price(amount)}\n"
        f"👤 User {telegram_id}\n\n"
        "This changes a real balance and is recorded in Activity Logs.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="apanel_wal_apply")],
            [InlineKeyboardButton("◀️ Cancel", callback_data="apanel_wallet")],
        ]))
    return WALLET_CONFIRM


async def wallet_adjust_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply the adjustment, tell the admin, and tell the customer."""
    from telegram.ext import ConversationHandler

    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Access denied.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    telegram_id = context.user_data.get('wallet_target')
    amount = context.user_data.get('wallet_amount')
    adding = context.user_data.get('wallet_adding', True)
    context.user_data.clear()

    if telegram_id is None or amount is None:
        await query.edit_message_text("❌ That adjustment expired. Start again.")
        return ConversationHandler.END

    delta = amount if adding else -amount
    result = await asyncio.to_thread(_adjust_balance_sync, telegram_id, delta,
                                     update.effective_user.id)

    back = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Back", callback_data="apanel_wallet")]])

    if result is None:
        await query.edit_message_text("❌ User not found.", reply_markup=back)
        return ConversationHandler.END
    if result[0] == "insufficient":
        await query.edit_message_text(
            f"❌ That would take the balance below zero.\n"
            f"Current: {format_price(result[1])}", reply_markup=back)
        return ConversationHandler.END

    old, new = result
    await query.edit_message_text(
        "✅ BALANCE UPDATED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {telegram_id}\n"
        f"{format_price(old)} → {format_price(new)}\n\n"
        "Recorded in Activity Logs.",
        reply_markup=back)

    # Tell the customer their balance moved; failing to reach them must not
    # undo an adjustment that already landed.
    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(f"{'➕' if adding else '➖'} Your balance was adjusted by an "
                  f"admin.\n💰 New balance: {format_price(new)}"))
    except Exception:
        logger.info("Could not notify user %s of a balance change", telegram_id)

    return ConversationHandler.END


async def wallet_adjust_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Leave the flow without changing anything."""
    from telegram.ext import ConversationHandler

    context.user_data.clear()
    return ConversationHandler.END
