"""The admin panel's two-column grid.

Layout is easy to break silently: adding one button to a menu re-pairs
every row after it, and a callback_data typo produces a button that looks
right and does nothing. These pin both.
"""

import pytest

from config.settings import settings
from fakes import set_switch
from handlers.admin_panel import admin_panel_main_markup
from utils.keyboards import (
    two_column_rows,
    create_admin_product_menu_keyboard,
    create_admin_category_menu_keyboard,
    create_admin_user_menu_keyboard,
    create_admin_order_menu_keyboard,
    create_admin_settings_menu_keyboard,
    create_admin_broadcast_menu_keyboard,
)

ADMIN_MENUS = [
    create_admin_product_menu_keyboard,
    create_admin_category_menu_keyboard,
    create_admin_user_menu_keyboard,
    create_admin_order_menu_keyboard,
    create_admin_settings_menu_keyboard,
    create_admin_broadcast_menu_keyboard,
]


def rows(keyboard):
    return [[b.text for b in row] for row in keyboard.inline_keyboard]


def test_two_column_rows_pairs_and_leaves_the_odd_one_alone():
    assert two_column_rows(["a", "b", "c", "d"]) == [["a", "b"], ["c", "d"]]
    assert two_column_rows(["a", "b", "c"]) == [["a", "b"], ["c"]]
    assert two_column_rows(["a"]) == [["a"]]
    assert two_column_rows([]) == []


@pytest.mark.parametrize("build", ADMIN_MENUS, ids=lambda f: f.__name__)
def test_no_admin_menu_row_exceeds_two_columns(build):
    assert all(len(row) <= 2 for row in build().inline_keyboard)


@pytest.mark.parametrize("build", ADMIN_MENUS, ids=lambda f: f.__name__)
def test_back_is_last_and_full_width(build):
    """Back is pressed by reflex; it must never share a row with an action."""
    last = build().inline_keyboard[-1]
    assert len(last) == 1
    assert last[0].text.startswith("🔙")


@pytest.mark.parametrize("build", ADMIN_MENUS, ids=lambda f: f.__name__)
def test_every_button_carries_callback_data(build):
    for row in build().inline_keyboard:
        for button in row:
            assert button.callback_data, f"{button.text} does nothing"


def test_the_panel_main_menu_is_a_two_column_grid():
    layout = admin_panel_main_markup().inline_keyboard

    assert all(len(row) <= 2 for row in layout)
    assert layout[-1][0].callback_data == "main_menu"
    assert layout[-1][0].text == "◀️ Back to Main Menu"


def test_category_adds_and_edits_line_up_in_the_same_columns():
    """Category left, subcategory right, on both rows."""
    layout = rows(create_admin_category_menu_keyboard())
    assert layout[0] == ["➕ Add Category", "➕ Add Subcategory"]
    assert layout[1] == ["✏️ Edit Category", "✏️ Edit Subcategory"]


def test_order_menu_separates_reading_from_changing():
    layout = rows(create_admin_order_menu_keyboard())
    assert layout[0] == ["📋 All Orders", "🚨 Disputes"]
    assert layout[1] == ["✅ Confirm Order", "❌ Cancel Order"]


def test_binance_kill_switch_never_shares_a_row(monkeypatch):
    """A half-width neighbour makes the one customer-facing switch mis-tappable."""
    from handlers import binance_admin as ba

    monkeypatch.setattr(settings, "BINANCE_PAY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_TEST_MODE", True, raising=False)
    monkeypatch.setattr(settings, "BINANCE_PAY_ID", "1", raising=False)
    set_switch("binance", True, monkeypatch)

    for row in ba._settings_keyboard().inline_keyboard:
        if any(b.callback_data == "binadmin_toggle" for b in row):
            assert len(row) == 1
            break
    else:
        pytest.fail("the toggle is missing from the Binance settings screen")


def test_admin_menu_callbacks_are_unique():
    """A duplicated callback_data means one of the two buttons is unreachable."""
    for build in ADMIN_MENUS:
        seen = [b.callback_data
                for row in build().inline_keyboard for b in row]
        assert len(seen) == len(set(seen)), f"{build.__name__} has a duplicate"
