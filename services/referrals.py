"""Refer & Earn: codes, attribution, and the one place a bonus is paid.

The reward is a fixed amount, paid to the referrer when the person they
brought in completes their first purchase. Paying on signup or on top-up
would both hand out money before the store has earned any - and paying on
signup in particular makes free balance out of spare Telegram accounts.

Three rules keep it from becoming a way to print money, and each is
enforced here rather than trusted to a caller:

  * A user cannot refer themselves.
  * Attribution happens once, at signup, from the deep link. An existing
    customer can never be claimed afterwards.
  * The payout is stamped on the referred user inside the same locked
    transaction that credits the referrer, so a second completed order
    finds it already paid.

All functions here are synchronous and expect to be called from a thread.
"""

import logging
import secrets
from datetime import datetime
from decimal import Decimal

from database import get_db_session, User, Settings as StoreSettings
from utils.money import to_money

logger = logging.getLogger(__name__)

# Unambiguous alphabet: no O/0, I/1, so a code read aloud or copied off a
# screenshot still works.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_MAX_ATTEMPTS = 10


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


def bonus_amount_sync() -> Decimal:
    """The configured bonus. Zero means Refer & Earn is switched off."""
    with get_db_session() as session:
        row = session.query(StoreSettings.referral_bonus).scalar()
    return to_money(row or 0)


def set_bonus_sync(amount, admin_telegram_id: int) -> Decimal:
    """Set the bonus. Zero switches the feature off."""
    from utils.audit import log_admin_action

    value = to_money(amount)
    with get_db_session() as session:
        row = session.query(StoreSettings).first()
        if row is None:
            row = StoreSettings()
            session.add(row)
        row.referral_bonus = value
        log_admin_action(session, admin_telegram_id, "referral_bonus_set",
                         target_type="settings", details=f"amount={value}")
        session.commit()
    return value


def ensure_code_sync(telegram_id: int):
    """The user's referral code, generating one the first time.

    Generated lazily rather than at signup, because most users never open
    the referral screen and an unused code is a row of noise.
    """
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return None
        if user.referral_code:
            return user.referral_code

        # secrets makes a collision vanishingly unlikely, but the column is
        # UNIQUE, so retry rather than raise into a handler.
        for _ in range(_MAX_ATTEMPTS):
            candidate = _new_code()
            taken = session.query(User.id).filter_by(
                referral_code=candidate).first()
            if taken:
                continue
            user.referral_code = candidate
            session.commit()
            return candidate

        logger.error("Could not allocate a referral code for %s", telegram_id)
        return None


def attribute_sync(telegram_id: int, code: str) -> bool:
    """Record who referred a brand new user. True when it stuck.

    Refuses on every path that would let someone pay themselves: an unknown
    code, their own code, a user who already has a referrer, or a user who
    has any history at all. The last one is what stops an existing customer
    being claimed by re-opening a deep link.
    """
    code = (code or "").strip().upper()
    if not code:
        return False

    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None or user.referred_by_id is not None:
            return False

        referrer = session.query(User).filter_by(referral_code=code).first()
        if referrer is None or referrer.id == user.id:
            return False

        # Only a user who has done nothing yet can be attributed. Anyone
        # with a balance, an order, or a code of their own is not new, and
        # letting them be claimed would mean paying for a customer the
        # referrer never brought.
        from database import Order

        has_history = (
            to_money(user.wallet_balance) > 0
            or user.referral_code is not None
            or session.query(Order.id).filter_by(user_id=user.id).first() is not None
        )
        if has_history:
            return False

        user.referred_by_id = referrer.id
        session.commit()
        return True


def pay_bonus_sync(telegram_id: int):
    """Pay the referrer for a user's first completed purchase.

    Returns (referrer_telegram_id, amount) when a bonus was paid, else None.
    Safe to call after every purchase: the second call finds
    referral_rewarded_at already stamped and does nothing. It also confirms
    the completed order itself, so it cannot pay out for a purchase that
    did not happen.
    """
    with get_db_session() as session:
        bonus = to_money(
            session.query(StoreSettings.referral_bonus).scalar() or 0)
        if bonus <= 0:
            return None

        # Lock the referred user: the stamp is what makes this idempotent,
        # so two concurrent purchases must not both read it as unset.
        user = (session.query(User)
                .filter_by(telegram_id=telegram_id)
                .with_for_update()
                .first())
        if user is None or user.referred_by_id is None:
            return None
        if user.referral_rewarded_at is not None:
            return None

        # Verify the purchase here rather than trusting the caller. This is
        # the function that moves money, and "called only after a completed
        # order" is a comment, not a guarantee - one future call site that
        # forgets would hand out balance for nothing.
        from database import Order, OrderStatus

        bought = (session.query(Order.id)
                  .filter(Order.user_id == user.id,
                          Order.status == OrderStatus.COMPLETED)
                  .first())
        if bought is None:
            return None

        referrer = (session.query(User)
                    .filter_by(id=user.referred_by_id)
                    .with_for_update()
                    .first())
        if referrer is None:
            return None
        # A banned referrer keeps the attribution but earns nothing.
        if referrer.is_banned:
            user.referral_rewarded_at = datetime.utcnow()
            session.commit()
            return None

        referrer.wallet_balance = to_money(referrer.wallet_balance + bonus)
        referrer.referral_earnings = to_money(
            (referrer.referral_earnings or 0) + bonus)
        user.referral_rewarded_at = datetime.utcnow()
        session.commit()

        return referrer.telegram_id, bonus


def stats_sync(telegram_id: int):
    """What the Refer & Earn screen shows: code, counts, earnings."""
    with get_db_session() as session:
        user = session.query(User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return None

        invited = session.query(User.id).filter_by(referred_by_id=user.id).count()
        # "Earned" counts only the ones that actually paid out, so the two
        # numbers on screen can differ and both be true.
        earned = (session.query(User.id)
                  .filter(User.referred_by_id == user.id,
                          User.referral_rewarded_at.isnot(None))
                  .count())
        bonus = to_money(
            session.query(StoreSettings.referral_bonus).scalar() or 0)

        return {
            'code': user.referral_code,
            'invited': invited,
            'rewarded': earned,
            'earnings': to_money(user.referral_earnings or 0),
            'bonus': bonus,
            'language': user.language,
        }
