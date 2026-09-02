"""The Admin Panel's navigation, its figures, and the one screen that
moves money.

Two things are worth pinning beyond "it renders": that no button in a
sixteen-section panel leads nowhere, and that the integration status never
claims a provider is connected when this codebase has no code for it.
"""

from decimal import Decimal

import pytest

from config.settings import settings
from database import (
    get_db_session, User, Order, Product, Transaction, AdminActionLog,
    OrderStatus, TransactionStatus, PaymentMethod, ProductType,
)
from handlers import admin_panel as ap
from fakes import (FakeUpdate, FakeQuery, FakeContext, FakeMessage,
                   FakeMessageUpdate)

ADMIN, CUSTOMER = 9001, 7001


@pytest.fixture(autouse=True)
def as_admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_IDS", {ADMIN}, raising=False)
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", ADMIN, raising=False)
    with get_db_session() as session:
        session.add_all([
            User(telegram_id=ADMIN, wallet_balance=Decimal("0")),
            User(telegram_id=CUSTOMER, username="buyer",
                 wallet_balance=Decimal("5.00")),
        ])


async def press(handler, data, user_id=ADMIN, context=None):
    query = FakeQuery(data=data, user_id=user_id)
    await handler(FakeUpdate(query, user_id), context or FakeContext())
    return query


def balance(telegram_id):
    with get_db_session() as session:
        return session.query(User.wallet_balance).filter_by(
            telegram_id=telegram_id).scalar()


# ------------------------------------------------------------ the panel

def test_the_main_menu_is_a_two_column_grid_with_back_last():
    layout = ap.admin_panel_main_markup().inline_keyboard

    assert all(len(row) <= 2 for row in layout[:-1])
    assert len(layout[-1]) == 1
    assert layout[-1][0].callback_data == "main_menu"


def test_every_requested_section_is_present():
    labels = [b.text for row in ap.admin_panel_main_markup().inline_keyboard
              for b in row]

    for expected in ("📊 Dashboard", "🛒 Orders", "📦 Products", "👥 Users",
                     "💳 Payments", "💰 Wallet", "🎁 Referrals", "🎟️ Coupons",
                     "📢 Broadcast", "🔔 Notifications", "📊 Reports",
                     "🔌 Integrations", "⚙️ Settings", "🔐 Security",
                     "📜 Activity Logs", "🛠️ System"):
        assert expected in labels, expected


def test_no_panel_button_leads_nowhere():
    """A sixteen-section panel is exactly where a dead button hides."""
    import bot
    from telegram.ext import CallbackQueryHandler, ConversationHandler

    app = bot.build_application()

    def first_handler(data):
        for handler in app.handlers[0]:
            if (isinstance(handler, CallbackQueryHandler) and handler.pattern
                    and handler.pattern.match(data)):
                return handler.callback.__name__
            if isinstance(handler, ConversationHandler):
                for entry in handler.entry_points:
                    if (isinstance(entry, CallbackQueryHandler) and entry.pattern
                            and entry.pattern.match(data)):
                        return entry.callback.__name__
        return None

    targets = {b.callback_data
               for row in ap.admin_panel_main_markup().inline_keyboard for b in row}
    for section in ap.SECTIONS.values():
        targets |= {data for _label, data in section.buttons}

    unhandled = sorted(t for t in targets if first_handler(t) is None)
    assert not unhandled, unhandled


@pytest.mark.parametrize("key", [k for k in ap.MENU_ORDER if k != "payments"])
async def test_every_section_renders(key):
    query = await press(ap.admin_panel_section, f"apanel_{key}")

    assert query.last_edit_text
    assert query.edits[-1][1].inline_keyboard[-1][0].text == "◀️ Back"


async def test_an_unknown_section_alerts_rather_than_going_quiet():
    query = await press(ap.admin_panel_section, "apanel_nonsense")

    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits


async def test_a_non_admin_reaches_nothing():
    query = await press(ap.admin_panel_menu, "admin_menu", user_id=123456)
    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits

    query = await press(ap.admin_panel_section, "apanel_dashboard", user_id=123456)
    assert not query.edits


# --------------------------------------------------------- the figures

async def test_the_dashboard_counts_real_rows():
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=CUSTOMER).first()
        session.add(Order(user_id=user.id, total_amount=Decimal("10.00"),
                          status=OrderStatus.COMPLETED))
        session.add(Transaction(user_id=user.id, amount=Decimal("10.00"),
                                payment_method=PaymentMethod.BINANCE_PAY,
                                status=TransactionStatus.MANUAL_REVIEW))
        session.add(Product(name="Netflix", price=Decimal("9.99"), stock_count=3,
                            product_type=ProductType.KEY, is_active=True))

    query = await press(ap.admin_panel_section, "apanel_dashboard")
    text = query.last_edit_text

    assert "Total: 2" in text            # two users
    assert "$10.00" in text              # revenue from the completed order
    assert "Manual review: 1" in text
    assert "Low stock: 1" in text


def test_an_uncoded_provider_is_never_shown_as_connected():
    """A green light for an integration that does not exist is a lie."""
    rows = dict(ap._integration_rows())

    for name, _why in ap.UNAVAILABLE:
        assert "🟢" not in rows[name]
        assert "no integration" in rows[name]


def test_the_security_screen_lists_admins_and_no_secrets(monkeypatch):
    monkeypatch.setattr(settings, "BINANCE_API_SECRET", "supersecret", raising=False)
    monkeypatch.setattr(settings, "BOT_TOKEN", "12345:tokentokentoken", raising=False)

    lines = "\n".join(__import__("asyncio").get_event_loop_policy().new_event_loop()
                      .run_until_complete(ap._security_body()))

    assert str(ADMIN) in lines
    assert "supersecret" not in lines
    assert "12345:tokentokentoken" not in lines


async def test_reports_render_for_every_window():
    for window in ("daily", "weekly", "monthly"):
        query = await press(ap.admin_panel_report, f"apanel_rep_{window}")
        assert "Revenue" in query.last_edit_text, window


async def test_an_unknown_report_alerts():
    query = await press(ap.admin_panel_report, "apanel_rep_yearly")

    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits


# ---------------------------------------------- manual balance changes

async def _adjust(amount, adding=True, target=CUSTOMER):
    context = FakeContext()
    await press(ap.wallet_adjust_start,
                "apanel_wal_add" if adding else "apanel_wal_deduct",
                context=context)
    await ap.wallet_adjust_target(
        FakeMessageUpdate(FakeMessage(text=str(target)), ADMIN), context)
    await ap.wallet_adjust_amount(
        FakeMessageUpdate(FakeMessage(text=amount), ADMIN), context)
    query = FakeQuery(data="apanel_wal_apply", user_id=ADMIN)
    await ap.wallet_adjust_apply(FakeUpdate(query, ADMIN), context)
    return query, context


async def test_adding_balance_credits_and_tells_the_customer():
    query, context = await _adjust("7.50")

    assert balance(CUSTOMER) == Decimal("12.50")
    assert "$5.00 → $12.50" in query.last_edit_text
    assert context.bot.sent[0][0] == CUSTOMER


async def test_deducting_balance_debits():
    await _adjust("2.00", adding=False)

    assert balance(CUSTOMER) == Decimal("3.00")


async def test_every_adjustment_is_audit_logged():
    await _adjust("1.00")

    with get_db_session() as session:
        # Read inside the session: the row detaches once it closes.
        details = session.query(AdminActionLog.details).filter_by(
            action="wallet_credit", admin_telegram_id=ADMIN).scalar()
    assert "5.00 -> 6.00" in details


async def test_an_adjustment_asks_before_it_moves_anything():
    """The confirmation is the point: this is a real balance."""
    context = FakeContext()
    await press(ap.wallet_adjust_start, "apanel_wal_add", context=context)
    await ap.wallet_adjust_target(
        FakeMessageUpdate(FakeMessage(text=str(CUSTOMER)), ADMIN), context)
    message = FakeMessage(text="7.50")
    await ap.wallet_adjust_amount(FakeMessageUpdate(message, ADMIN), context)

    assert balance(CUSTOMER) == Decimal("5.00")          # untouched so far
    labels = [b.text for row in message.replies[-1][1].inline_keyboard for b in row]
    assert labels == ["✅ Confirm", "◀️ Cancel"]


async def test_over_deducting_refuses_rather_than_flooring_at_zero():
    """A wallet that quietly floors hides an admin's miscount."""
    query, _context = await _adjust("999.00", adding=False)

    assert balance(CUSTOMER) == Decimal("5.00")
    assert "below zero" in query.last_edit_text


async def test_an_unknown_telegram_id_is_refused():
    context = FakeContext()
    await press(ap.wallet_adjust_start, "apanel_wal_add", context=context)
    message = FakeMessage(text="404404")

    state = await ap.wallet_adjust_target(
        FakeMessageUpdate(message, ADMIN), context)

    assert state == ap.WALLET_TARGET
    assert "No user" in message.replies[-1][0]


async def test_a_non_admin_cannot_start_an_adjustment():
    query = await press(ap.wallet_adjust_start, "apanel_wal_add", user_id=123456)

    assert query.answers[0][1].get("show_alert") is True
    assert balance(CUSTOMER) == Decimal("5.00")


async def test_a_non_admin_cannot_apply_one():
    context = FakeContext()
    context.user_data.update({'wallet_adding': True, 'wallet_target': CUSTOMER,
                              'wallet_amount': Decimal("100")})
    query = FakeQuery(data="apanel_wal_apply", user_id=123456)

    await ap.wallet_adjust_apply(FakeUpdate(query, 123456), context)

    assert balance(CUSTOMER) == Decimal("5.00")
