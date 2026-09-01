"""Admin action audit trail.

log_admin_action() is called from the handlers that ban/unban users, cancel
or reactivate orders, confirm or cancel payments, resolve disputes, restock
keys, and edit product prices - the actions an admin (especially on a bot
with more than one) would plausibly need to explain later. It's a thin
wrapper: it just adds a row to the session you're already inside, so it
commits (or rolls back) together with whatever else that handler is doing.
"""

from database import AdminActionLog


def log_admin_action(session, admin_telegram_id: int, action: str,
                      target_type: str = None, target_id: int = None,
                      details: str = None) -> None:
    """Record one admin action. Call this before the enclosing
    `with get_db_session() as session:` block exits - it doesn't commit on
    its own, so it lives or dies with the rest of that transaction.
    """
    session.add(AdminActionLog(
        admin_telegram_id=admin_telegram_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    ))
