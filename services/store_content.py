"""Which optional main-menu features this store has switched on.

Cached in this process for the same reason the payment switches are: the
main menu is built on the event loop, by eleven different call sites, and
none of them should be doing a database read to decide whether one button
exists. Terms change roughly never, so a value loaded at startup and
updated when an admin saves is accurate in practice.
"""

import logging

from database import get_db_session, Settings as StoreSettings
from utils.audit import log_admin_action

logger = logging.getLogger(__name__)

_has_terms = False
_has_referrals = False


def has_terms() -> bool:
    """Whether to offer the Terms button at all."""
    return _has_terms


def has_referrals() -> bool:
    """Whether to offer Refer & Earn - i.e. whether a bonus is configured.

    A bonus of zero means the store has not chosen to give money away, so
    the button would lead to a screen promising nothing.
    """
    return _has_referrals


def read_sync() -> bool:
    """Load the flags from the database. Call from a thread."""
    global _has_terms, _has_referrals
    with get_db_session() as session:
        row = session.query(StoreSettings.terms_text, StoreSettings.faq_text,
                            StoreSettings.referral_bonus).first()
        terms, faq, bonus = (row if row else (None, None, None))
        _has_terms = bool((terms or "").strip() or (faq or "").strip())
        _has_referrals = bool(bonus and bonus > 0)
    return _has_terms


# The two pages behind Terms & FAQ, and the Settings column each lives in.
PAGES = {"terms": "terms_text", "faq": "faq_text"}


def get_page_sync(page: str):
    """One page's text, or None."""
    column = getattr(StoreSettings, PAGES[page])
    with get_db_session() as session:
        return session.query(column).scalar()


def set_page_sync(page: str, text, admin_telegram_id: int) -> bool:
    """Save one page and update the cache. Call from a thread.

    An empty string clears it. The menu button follows whether EITHER page
    has content, so a store can publish just an FAQ.
    """
    global _has_terms
    cleaned = (text or "").strip() or None

    with get_db_session() as session:
        row = session.query(StoreSettings).first()
        if row is None:
            row = StoreSettings()
            session.add(row)
        setattr(row, PAGES[page], cleaned)
        log_admin_action(
            session, admin_telegram_id,
            f"store_{page}_set" if cleaned else f"store_{page}_clear",
            target_type="settings",
        )
        session.commit()
        _has_terms = any(
            (getattr(row, column) or "").strip() for column in PAGES.values())

    return cleaned is not None


def set_referral_bonus_cache(enabled: bool) -> None:
    """Keep the cache honest after an admin changes the bonus."""
    global _has_referrals
    _has_referrals = enabled


def refresh() -> bool:
    """Load the flags at startup, before any menu is built."""
    try:
        return read_sync()
    except Exception:
        # A settings table that predates the column, or an unreachable
        # database at boot. Hiding the button is the safe direction: it
        # cannot send anyone to an empty page.
        logger.warning("Could not load the store content flags - hiding the "
                       "optional buttons", exc_info=True)
        return _has_terms
