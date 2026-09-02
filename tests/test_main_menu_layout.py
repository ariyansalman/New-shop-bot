"""The user main menu, its exact structure, and the two new destinations.

The Admin Panel entry is the one that matters for correctness: hiding a
button is presentation, so the test checks both that normal users cannot
see it AND that the handler behind it refuses them anyway.
"""

from decimal import Decimal

import pytest

from config.settings import settings
from database import get_db_session, User, Product, ProductType
from services import store_content
from handlers import user_handlers as uh
from handlers import admin_handlers
from utils import create_main_menu_keyboard, create_terms_menu_keyboard
from fakes import (FakeUpdate, FakeQuery, FakeContext, FakeMessage,
                   FakeMessageUpdate)

USER, ADMIN = 5001, 9001


@pytest.fixture(autouse=True)
def store(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_IDS", {ADMIN}, raising=False)
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", ADMIN, raising=False)
    monkeypatch.setattr(store_content, "_has_terms", True, raising=False)
    monkeypatch.setattr(store_content, "_has_referrals", True, raising=False)
    from utils import helpers
    helpers._ban_cache.clear()
    with get_db_session() as session:
        session.add(User(telegram_id=USER, wallet_balance=Decimal("10")))


def rows(keyboard):
    return [[(b.text, b.callback_data) for b in row]
            for row in keyboard.inline_keyboard]


def menu(is_admin_user=False):
    return create_main_menu_keyboard('en', is_admin_user=is_admin_user,
                                     has_terms=True, has_referrals=True)


# ----------------------------------------------------------- the layout

def test_the_menu_matches_the_specified_structure():
    assert rows(menu()) == [
        [("🛒 Products", "products")],
        [("🔎 Search Products", "search")],
        [("💰 Top Up", "topup"), ("📋 Order History", "order_history")],
        [("👥 Refer & Earn", "referral"), ("👤 My Account", "account")],
        [("🧮 Availability", "availability"), ("📞 Support", "support")],
        [("📄 Terms & FAQ", "terms")],
        [("🌐 Language", "language")],
    ]


def test_terms_language_and_admin_stay_full_width():
    layout = menu(is_admin_user=True).inline_keyboard
    full_width = {row[0].callback_data for row in layout if len(row) == 1}

    assert {"terms", "language", "admin_menu"} <= full_width


def test_the_paired_rows_are_exactly_two_wide():
    for row in menu().inline_keyboard:
        assert len(row) <= 2


def test_no_button_is_duplicated():
    seen = [b.callback_data for row in menu(is_admin_user=True).inline_keyboard
            for b in row]
    assert len(seen) == len(set(seen))


# ------------------------------------------------------ admin visibility

def test_a_normal_user_does_not_see_the_admin_panel():
    assert "admin_menu" not in [b.callback_data for row in menu().inline_keyboard
                                for b in row]


def test_an_admin_sees_the_admin_panel_last():
    layout = menu(is_admin_user=True).inline_keyboard

    assert layout[-1][0].callback_data == "admin_menu"
    assert layout[-1][0].text == "👑 Admin Panel"


async def test_the_admin_handler_refuses_a_normal_user_anyway():
    """Hiding the button is presentation; this is the permission check."""
    query = FakeQuery(data="admin_menu", user_id=USER)

    await admin_handlers.admin_menu_callback(FakeUpdate(query, USER), FakeContext())

    assert query.answers[0][1].get("show_alert") is True
    assert not query.edits


# ------------------------------------------------------------ Terms & FAQ

def test_the_terms_section_has_both_pages_and_a_back():
    assert rows(create_terms_menu_keyboard()) == [
        [("📜 Terms & Conditions", "terms_conditions")],
        [("❓ Frequently Asked Questions", "terms_faq")],
        [("◀️ Back", "main_menu")],
    ]


async def test_a_terms_page_backs_out_to_the_section_not_the_menu():
    query = FakeQuery(data="terms_conditions", user_id=USER)
    await uh.terms_page_callback(FakeUpdate(query, USER), FakeContext())

    back = query.edits[-1][1].inline_keyboard[-1][0]
    assert back.callback_data == "terms"


async def test_the_two_pages_are_independent():
    await __import__("asyncio").to_thread(
        store_content.set_page_sync, "faq", "Delivery is instant.", ADMIN)

    query = FakeQuery(data="terms_faq", user_id=USER)
    await uh.terms_page_callback(FakeUpdate(query, USER), FakeContext())
    assert "Delivery is instant." in query.last_edit_text

    query = FakeQuery(data="terms_conditions", user_id=USER)
    await uh.terms_page_callback(FakeUpdate(query, USER), FakeContext())
    assert "Not published yet" in query.last_edit_text


async def test_an_faq_alone_is_enough_to_show_the_menu_button():
    await __import__("asyncio").to_thread(
        store_content.set_page_sync, "faq", "Delivery is instant.", ADMIN)

    assert store_content.has_terms() is True


# ----------------------------------------------------------------- search

def make_products(*names):
    with get_db_session() as session:
        for name in names:
            session.add(Product(name=name, price=Decimal("9.99"), stock_count=5,
                                product_type=ProductType.KEY, is_active=True))


async def search(text):
    message = FakeMessage(text=text)
    await uh.search_results(FakeMessageUpdate(message, USER), FakeContext())
    return message.replies[-1]


async def test_search_asks_before_it_searches():
    query = FakeQuery(data="search", user_id=USER)

    state = await uh.search_start(FakeUpdate(query, USER), FakeContext())

    assert state == uh.SEARCH_QUERY
    assert "search" in query.last_edit_text.lower()


async def test_search_finds_by_part_of_a_name():
    make_products("Netflix Premium", "Netflix Basic", "Spotify Family")

    text, markup = await search("netflix")

    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert sum("Netflix" in label for label in labels) == 2
    assert not any("Spotify" in label for label in labels)


async def test_search_is_case_insensitive():
    make_products("Netflix Premium")

    _text, markup = await search("NETFLIX")

    assert any("Netflix" in b.text for row in markup.inline_keyboard for b in row)


async def test_a_result_opens_the_normal_product_page():
    """Search must not become a second product-detail implementation."""
    make_products("Netflix Premium")

    _text, markup = await search("netflix")

    assert markup.inline_keyboard[0][0].callback_data.startswith("product_")


async def test_search_ignores_inactive_products():
    make_products("Netflix Premium")
    with get_db_session() as session:
        session.query(Product).update({"is_active": False})

    text, _markup = await search("netflix")

    assert "Nothing matched" in text


async def test_a_wildcard_does_not_dump_the_catalogue():
    """% and _ are LIKE wildcards; unescaped, "%" would list everything."""
    make_products("Netflix Premium", "Spotify Family")

    for probe in ("%%", "___"):
        text, _markup = await search(probe)
        assert "Nothing matched" in text, probe


async def test_a_too_short_query_is_asked_again():
    make_products("Netflix Premium")
    message = FakeMessage(text="n")

    state = await uh.search_results(FakeMessageUpdate(message, USER), FakeContext())

    assert state == uh.SEARCH_QUERY


async def test_no_match_says_so_and_offers_a_way_back():
    make_products("Netflix Premium")

    text, markup = await search("zzzzz")

    assert "Nothing matched" in text
    assert markup.inline_keyboard[-1][0].callback_data == "main_menu"


# -------------------------------------------------------------- account

async def test_the_old_wallet_callback_still_reaches_my_account():
    """A message already on someone's phone must not become a dead button."""
    import bot
    from telegram.ext import CallbackQueryHandler

    app = bot.build_application()
    matched = [h.callback.__name__ for h in app.handlers[0]
               if isinstance(h, CallbackQueryHandler)
               and h.pattern and h.pattern.match("wallet")]

    assert matched == ["account_callback"]
