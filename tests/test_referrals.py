"""Refer & Earn.

This is the only feature that gives money away, so most of these tests are
about the ways it must refuse. The payout is stamped on the referred user
inside the same locked transaction as the credit, and pay_bonus_sync
confirms the completed order itself rather than trusting its caller.
"""

from decimal import Decimal

import pytest

from database import (
    get_db_session, User, Order, OrderStatus, Settings as StoreSettings,
    AdminActionLog,
)
from services import referrals, store_content
from handlers import user_handlers as uh
from utils import create_main_menu_keyboard
from fakes import FakeUpdate, FakeQuery, FakeContext

ALICE, BOB, CAROL = 1001, 1002, 1003


@pytest.fixture(autouse=True)
def bonus_on(monkeypatch):
    monkeypatch.setattr(store_content, "_has_referrals", True, raising=False)
    from utils import helpers
    helpers._ban_cache.clear()
    with get_db_session() as session:
        session.add(StoreSettings(referral_bonus=Decimal("1.50")))


def make(telegram_id, balance="0.00"):
    with get_db_session() as session:
        user = User(telegram_id=telegram_id, wallet_balance=Decimal(balance))
        session.add(user)
        session.flush()
        return user.id


def buy(user_db_id, status=OrderStatus.COMPLETED):
    with get_db_session() as session:
        session.add(Order(user_id=user_db_id, total_amount=Decimal("10.00"),
                          status=status))


def balance(telegram_id):
    with get_db_session() as session:
        return session.query(User.wallet_balance).filter_by(
            telegram_id=telegram_id).scalar()


def link(referrer=ALICE, referred=BOB):
    """Alice refers Bob."""
    code = referrals.ensure_code_sync(referrer)
    assert referrals.attribute_sync(referred, code) is True
    return code


# ------------------------------------------------------------------ codes

def test_a_code_is_stable_once_generated():
    make(ALICE)

    first = referrals.ensure_code_sync(ALICE)
    assert first
    assert referrals.ensure_code_sync(ALICE) == first


def test_codes_avoid_characters_that_are_read_wrong():
    """A code gets copied off a screenshot or read aloud."""
    make(ALICE)

    code = referrals.ensure_code_sync(ALICE)
    assert not set(code) & set("O0I1")


def test_two_users_get_different_codes():
    make(ALICE)
    make(BOB)

    assert referrals.ensure_code_sync(ALICE) != referrals.ensure_code_sync(BOB)


# ------------------------------------------------------------ attribution

def test_a_new_user_can_be_attributed():
    make(ALICE)
    make(BOB)

    assert referrals.attribute_sync(BOB, referrals.ensure_code_sync(ALICE)) is True


def test_nobody_can_refer_themselves():
    make(ALICE)

    assert referrals.attribute_sync(ALICE, referrals.ensure_code_sync(ALICE)) is False


def test_an_unknown_code_is_refused():
    make(BOB)

    assert referrals.attribute_sync(BOB, "NOTACODE") is False


def test_a_user_cannot_be_re_attributed():
    """Otherwise a second link overwrites who gets paid."""
    make(ALICE)
    make(BOB)
    make(CAROL)
    link(ALICE, BOB)

    assert referrals.attribute_sync(BOB, referrals.ensure_code_sync(CAROL)) is False


def test_an_existing_customer_cannot_be_claimed():
    """Re-opening a deep link must not hand someone else's customer away."""
    make(ALICE)
    bob_id = make(BOB)
    buy(bob_id)

    assert referrals.attribute_sync(BOB, referrals.ensure_code_sync(ALICE)) is False


def test_a_user_with_a_balance_is_not_new():
    make(ALICE)
    make(BOB, balance="5.00")

    assert referrals.attribute_sync(BOB, referrals.ensure_code_sync(ALICE)) is False


def test_the_code_is_matched_case_insensitively():
    """People retype links in lowercase."""
    make(ALICE)
    make(BOB)
    code = referrals.ensure_code_sync(ALICE)

    assert referrals.attribute_sync(BOB, code.lower()) is True


# ---------------------------------------------------------------- payout

def test_nothing_is_paid_before_a_purchase():
    make(ALICE)
    make(BOB)
    link()

    assert referrals.pay_bonus_sync(BOB) is None
    assert balance(ALICE) == Decimal("0.00")


def test_a_cancelled_order_does_not_count():
    make(ALICE)
    bob_id = make(BOB)
    link()
    buy(bob_id, status=OrderStatus.CANCELLED)

    assert referrals.pay_bonus_sync(BOB) is None
    assert balance(ALICE) == Decimal("0.00")


def test_a_completed_purchase_pays_the_referrer():
    make(ALICE)
    bob_id = make(BOB)
    link()
    buy(bob_id)

    paid = referrals.pay_bonus_sync(BOB)

    assert paid == (ALICE, Decimal("1.50"))
    assert balance(ALICE) == Decimal("1.50")


def test_the_bonus_is_paid_exactly_once():
    """The guard that stops a referral becoming an income stream."""
    make(ALICE)
    bob_id = make(BOB)
    link()
    buy(bob_id)
    referrals.pay_bonus_sync(BOB)

    for _ in range(5):
        assert referrals.pay_bonus_sync(BOB) is None

    assert balance(ALICE) == Decimal("1.50")


def test_a_zero_bonus_pays_nothing():
    """Refer & Earn switched off must not pay retroactively."""
    make(ALICE)
    bob_id = make(BOB)
    link()
    buy(bob_id)
    referrals.set_bonus_sync(Decimal("0"), 1)

    assert referrals.pay_bonus_sync(BOB) is None
    assert balance(ALICE) == Decimal("0.00")


def test_an_unreferred_buyer_pays_nobody():
    make(ALICE)
    bob_id = make(BOB)
    buy(bob_id)

    assert referrals.pay_bonus_sync(BOB) is None


def test_a_banned_referrer_earns_nothing_but_is_settled():
    """Left unstamped it would retry on every future purchase."""
    make(ALICE)
    bob_id = make(BOB)
    link()
    with get_db_session() as session:
        session.query(User).filter_by(telegram_id=ALICE).update({"is_banned": True})
    buy(bob_id)

    assert referrals.pay_bonus_sync(BOB) is None
    assert balance(ALICE) == Decimal("0.00")
    with get_db_session() as session:
        stamped = session.query(User.referral_rewarded_at).filter_by(
            telegram_id=BOB).scalar()
    assert stamped is not None


def test_earnings_accumulate_across_referrals():
    make(ALICE)
    bob_id = make(BOB)
    carol_id = make(CAROL)
    link(ALICE, BOB)
    link(ALICE, CAROL)
    buy(bob_id)
    buy(carol_id)

    referrals.pay_bonus_sync(BOB)
    referrals.pay_bonus_sync(CAROL)

    assert balance(ALICE) == Decimal("3.00")
    with get_db_session() as session:
        assert session.query(User.referral_earnings).filter_by(
            telegram_id=ALICE).scalar() == Decimal("3.00")


# ----------------------------------------------------------------- admin

def test_setting_the_bonus_is_audit_logged():
    referrals.set_bonus_sync(Decimal("2.00"), 4242)

    with get_db_session() as session:
        assert session.query(AdminActionLog).filter_by(
            action="referral_bonus_set", admin_telegram_id=4242).count() == 1


def test_the_menu_button_follows_the_bonus(monkeypatch):
    monkeypatch.setattr(store_content, "_has_referrals", False, raising=False)
    labels = [b.callback_data for row in
              create_main_menu_keyboard().inline_keyboard for b in row]
    assert "referral" not in labels

    monkeypatch.setattr(store_content, "_has_referrals", True, raising=False)
    labels = [b.callback_data for row in
              create_main_menu_keyboard().inline_keyboard for b in row]
    assert "referral" in labels


def test_the_flags_reload_from_the_database():
    referrals.set_bonus_sync(Decimal("0"), 1)
    store_content.read_sync()
    assert store_content.has_referrals() is False

    referrals.set_bonus_sync(Decimal("1.00"), 1)
    store_content.read_sync()
    assert store_content.has_referrals() is True


# ---------------------------------------------------------------- screen

async def test_the_screen_shows_the_link_and_the_totals(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "BOT_USERNAME", "DemoStoreBot", raising=False)
    make(ALICE)
    bob_id = make(BOB)
    link()
    buy(bob_id)
    referrals.pay_bonus_sync(BOB)

    query = FakeQuery(data="referral", user_id=ALICE)
    await uh.referral_callback(FakeUpdate(query, ALICE), FakeContext())

    code = referrals.ensure_code_sync(ALICE)
    assert f"https://t.me/DemoStoreBot?start={code}" in query.last_edit_text
    assert "Invited: 1" in query.last_edit_text
    assert "$1.50" in query.last_edit_text


async def test_the_screen_still_works_without_a_bot_username(monkeypatch):
    """BOT_USERNAME is optional, so the code alone has to be usable."""
    from config.settings import settings
    monkeypatch.setattr(settings, "BOT_USERNAME", "", raising=False)
    make(ALICE)

    query = FakeQuery(data="referral", user_id=ALICE)
    await uh.referral_callback(FakeUpdate(query, ALICE), FakeContext())

    assert referrals.ensure_code_sync(ALICE) in query.last_edit_text
    labels = [b.text for row in query.edits[-1][1].inline_keyboard for b in row]
    assert not any("Share" in text for text in labels)


async def test_opening_the_screen_creates_a_code():
    make(ALICE)

    query = FakeQuery(data="referral", user_id=ALICE)
    await uh.referral_callback(FakeUpdate(query, ALICE), FakeContext())

    with get_db_session() as session:
        assert session.query(User.referral_code).filter_by(
            telegram_id=ALICE).scalar()
