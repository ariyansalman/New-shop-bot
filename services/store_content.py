"""Whether the store has written its own Terms & FAQ.

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


def has_terms() -> bool:
    """Whether to offer the Terms button at all."""
    return _has_terms


def read_sync() -> bool:
    """Load the flag from the database. Call from a thread."""
    global _has_terms
    with get_db_session() as session:
        text = session.query(StoreSettings.terms_text).scalar()
        _has_terms = bool(text and text.strip())
    return _has_terms


def get_terms_sync():
    """The terms text itself, or None."""
    with get_db_session() as session:
        return session.query(StoreSettings.terms_text).scalar()


def set_terms_sync(text, admin_telegram_id: int) -> bool:
    """Save the terms and update the cache. Call from a thread.

    An empty string clears them, which also hides the button again.
    """
    global _has_terms
    cleaned = (text or "").strip() or None

    with get_db_session() as session:
        row = session.query(StoreSettings).first()
        if row is None:
            row = StoreSettings()
            session.add(row)
        row.terms_text = cleaned
        log_admin_action(
            session, admin_telegram_id,
            "store_terms_set" if cleaned else "store_terms_clear",
            target_type="settings",
        )
        session.commit()

    _has_terms = cleaned is not None
    return _has_terms


def refresh() -> bool:
    """Load the flag at startup, before any menu is built."""
    try:
        return read_sync()
    except Exception:
        # A settings table that predates the column, or an unreachable
        # database at boot. Hiding the button is the safe direction: it
        # cannot send anyone to an empty page.
        logger.warning("Could not load the Terms flag - hiding the button",
                       exc_info=True)
        return _has_terms
