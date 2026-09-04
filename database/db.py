"""Database connection and session management.

Supports both SQLite (local development) and PostgreSQL/Supabase (production).
The engine is configured differently for each: SQLite needs pragmas and
thread-safety flags, PostgreSQL needs pool sizing and keepalives.
"""

import logging
import os

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import NullPool
from contextlib import contextmanager

from config.settings import settings
from database.models import Base

logger = logging.getLogger(__name__)

IS_SQLITE = settings.DATABASE_URL.startswith('sqlite')
IS_POSTGRES = settings.DATABASE_URL.startswith('postgresql')

# Supabase's *transaction* pooler listens on 6543 and multiplexes connections,
# so client-side pooling on top of it causes stale-connection errors. Detect it
# and let PgBouncer do the pooling instead.
USING_TRANSACTION_POOLER = IS_POSTGRES and ':6543/' in settings.DATABASE_URL

_engine_kwargs = {
    "echo": settings.DB_ECHO,
    # Verify a connection is alive before handing it out. Essential against
    # Supabase/Railway idle timeouts, which otherwise surface as random
    # "server closed the connection unexpectedly" errors.
    "pool_pre_ping": True,
}

if IS_SQLITE:
    # Payment jobs touch the DB from worker threads (asyncio.to_thread), and a
    # longer lock timeout helps when the webhook writes concurrently.
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
elif IS_POSTGRES:
    _engine_kwargs["connect_args"] = {
        # Notice a dead TCP connection instead of hanging until the OS gives up.
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": 10,
    }
    if USING_TRANSACTION_POOLER:
        _engine_kwargs["poolclass"] = NullPool
    else:
        _engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
        _engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
        _engine_kwargs["pool_recycle"] = settings.DB_POOL_RECYCLE
        _engine_kwargs["pool_timeout"] = 30

engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable FK enforcement and WAL on SQLite (both off by default)."""
    if not IS_SQLITE:
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
    except Exception:
        # Not fatal, but not nothing either: without foreign_keys=ON,
        # SQLite silently accepts rows the schema forbids, so the tests
        # would pass on data Postgres would reject.
        logger.warning("Could not set SQLite pragmas", exc_info=True)


# Create session factory
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)


def _stamp_alembic_head():
    """Record that a freshly created schema is already at the latest revision.

    create_all() builds the current models directly, so the schema matches
    head - but Alembic doesn't know that, and would then try to run the
    baseline migration's CREATE TABLEs against tables that already exist
    ("table broadcasts already exists"), leaving migrations permanently
    stuck for that database. Stamping closes that gap.
    """
    from alembic import command

    cfg = _alembic_config()
    if cfg is None:
        logger.warning("alembic.ini not found - skipping stamp of the new database")
        return

    command.stamp(cfg, "head")
    logger.info("New database stamped at Alembic head")


def _alembic_config():
    """Build an Alembic config pointed at this process's database."""
    from alembic.config import Config

    alembic_ini = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic.ini"
    )
    if not os.path.exists(alembic_ini):
        return None

    cfg = Config(alembic_ini)
    # env.py reads the URL from settings, but set it explicitly so this can
    # never act on a different database than the one just inspected.
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    # Keep Alembic's logging config out of this process: env.py's
    # fileConfig() would disable every logger the app already created (see
    # the comment there). Only the CLI should own logging configuration.
    cfg.attributes["configure_logger"] = False
    return cfg


def _column_names(inspector, table: str) -> set:
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _index_names(inspector, table: str) -> set:
    if not inspector.has_table(table):
        return set()
    return {i["name"] for i in inspector.get_indexes(table)}


def _column(inspector, table: str, column: str):
    if not inspector.has_table(table):
        return None
    for c in inspector.get_columns(table):
        if c["name"] == column:
            return c
    return None


# What each migration leaves behind, oldest first. Used to work out how far
# an UNSTAMPED database has already come - see _detect_stamp_point().
_MIGRATION_PROBES = [
    # 074df6640eae: money columns Float -> Numeric(12,2)
    ("074df6640eae", lambda i: "NUMERIC" in str(
        (_column(i, "users", "wallet_balance") or {}).get("type", "")).upper()),
    # aca145102282: admin action audit trail
    ("aca145102282", lambda i: i.has_table("admin_action_logs")),
    # 66b1e79055b5: per-user language preference
    ("66b1e79055b5", lambda i: "language" in _column_names(i, "users")),
    # b7c41a9d2e10: order_items.product_id made nullable
    ("b7c41a9d2e10", lambda i: bool(
        (_column(i, "order_items", "product_id") or {}).get("nullable"))),
    # c8d5f2a41b93: Binance provider fields on transactions
    ("c8d5f2a41b93", lambda i: "provider" in _column_names(i, "transactions")),
    # d3f6b18c4a27: Binance kill switch on settings
    ("d3f6b18c4a27", lambda i: "binance_pay_enabled" in _column_names(i, "settings")),
    # e1a4c7b83f52: per-product delivery instructions
    ("e1a4c7b83f52", lambda i: "delivery_instructions" in _column_names(i, "products")),
    # f2b9d61c8a74: settings.binance_pay_enabled made nullable
    ("f2b9d61c8a74", lambda i: bool(
        (_column(i, "settings", "binance_pay_enabled") or {}).get("nullable"))),
    # a3c8e5f10b76: admin switches for CryptoBot and Card
    ("a3c8e5f10b76", lambda i: "card_pay_enabled" in _column_names(i, "settings")),
    # b6d2f94e15c8: the store's Terms & FAQ text
    ("b6d2f94e15c8", lambda i: "terms_text" in _column_names(i, "settings")),
    # c7e83a1d4b09: Refer & Earn
    ("c7e83a1d4b09", lambda i: "referral_code" in _column_names(i, "users")),
    # d9f47b23e6a1: the FAQ page
    ("d9f47b23e6a1", lambda i: "faq_text" in _column_names(i, "settings")),
    # e5b19c74d3a2: indexes for the hot read paths
    ("e5b19c74d3a2", lambda i: "ix_orders_status" in _index_names(i, "orders")),
]

# The revision an unstamped database is at when none of the probes pass.
_BASELINE_REVISION = "96e65c626176"


def _detect_stamp_point(inspector) -> str:
    """Work out which revision an unstamped existing database is really at.

    A database built by create_all() before Alembic existed - or by any boot
    that skipped the migration step - has tables but no alembic_version row.
    Stamping it at head would skip migrations it genuinely still needs;
    stamping it at the baseline would re-run CREATE TABLEs against tables
    that already exist. Both leave migrations permanently stuck.

    So instead of guessing, look at the schema. Walk the migrations oldest
    first and stop at the first one whose changes are NOT present: everything
    before it has been applied, everything from it on has not. Stopping at
    the first gap (rather than taking the newest artifact found) is the safe
    direction - it can only ever re-apply a migration, never skip one.
    """
    applied = _BASELINE_REVISION
    for revision, probe in _MIGRATION_PROBES:
        try:
            present = probe(inspector)
        except Exception:
            present = False
        if not present:
            break
        applied = revision
    return applied


def _recorded_revision(inspector):
    """The revision in alembic_version, or None when there is no version row."""
    from sqlalchemy import text

    if not inspector.has_table("alembic_version"):
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:
        logger.warning("Could not read alembic_version", exc_info=True)
        return None
    return row[0] if row else None


def _revision_exists(cfg, revision: str) -> bool:
    """Whether this project actually ships the given revision."""
    from alembic.script import ScriptDirectory

    try:
        ScriptDirectory.from_config(cfg).get_revision(revision)
        return True
    except Exception:
        return False


def _sync_schema(inspector) -> None:
    """Bring an existing database up to the latest revision.

    The app does this itself rather than trusting a platform release step:
    a deployment whose release command silently does not run boots against a
    schema that is missing columns the code selects, and dies on the first
    query with a raw driver error. Doing it here means the schema is correct
    by the time anything reads from it, on every host.

    Safe for this project because it deploys as a single replica (see
    DEPLOY.md) - two instances migrating the same database concurrently is
    not something Alembic protects against.
    """
    from alembic import command

    cfg = _alembic_config()
    if cfg is None:
        logger.warning("alembic.ini not found - skipping the schema upgrade")
        return

    recorded = _recorded_revision(inspector)

    if recorded is None:
        reason = "has tables but no Alembic version row"
    elif not _revision_exists(cfg, recorded):
        # The version row names a revision this project has never had - a
        # database previously managed by a different Alembic setup (one that
        # numbered its revisions 0001, 0002, ...). Alembic cannot upgrade
        # from a revision it cannot resolve, so the row has to be replaced.
        reason = f"is stamped {recorded!r}, which is not a revision of this project"
    else:
        command.upgrade(cfg, "head")
        logger.info("Database schema is at the latest revision")
        return

    stamp_at = _detect_stamp_point(inspector)
    logger.warning(
        "Database %s. Detected it at revision %s from the schema itself; "
        "stamping and upgrading from there.", reason, stamp_at,
    )
    # purge drops the existing version row first: stamping on top of an
    # unresolvable one would leave both behind and read as a branched head.
    command.stamp(cfg, stamp_at, purge=True)

    command.upgrade(cfg, "head")
    logger.info("Database schema is at the latest revision")


def init_db():
    """Create missing tables and bring the schema up to the latest revision.

    Safe to run on every boot. create_all() only creates what does not exist
    and never alters an existing table, so the Alembic upgrade afterwards is
    what actually applies column changes to a database that already has data
    in it.

    A completely empty database is stamped at head instead: create_all() has
    just built the current schema directly, so running the migrations over
    it would fail on tables that already exist.
    """
    inspector = inspect(engine)
    # alembic_version alone doesn't count: an existing database that predates
    # Alembic has tables but no version row, and stamping that one blind
    # could skip migrations it genuinely still needs.
    fresh = not inspector.has_table("users") and not inspector.has_table("alembic_version")

    Base.metadata.create_all(engine)
    logger.info("Database tables created successfully")

    if fresh:
        try:
            _stamp_alembic_head()
        except Exception:
            # Not fatal: the schema is correct either way, and `alembic stamp
            # head` can still be run by hand (see DEPLOY.md).
            logger.exception("Could not stamp the new database at Alembic head")
        return

    try:
        # Re-inspect: create_all() may have added tables, and the cached
        # inspector would not know about them.
        _sync_schema(inspect(engine))
    except Exception:
        # Fatal on purpose. Booting on a schema the code cannot query just
        # moves the failure to the first user who touches the wrong table,
        # as an unreadable driver error instead of this one.
        logger.exception(
            "Could not bring the database schema up to date. The bot would "
            "run against a schema its queries do not match, so it is stopping "
            "here. Run `alembic upgrade head` against DATABASE_URL and check "
            "the error above."
        )
        raise


def check_connection() -> bool:
    """Verify the database is reachable, with a readable error if it is not."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        backend = "Supabase/PostgreSQL" if IS_POSTGRES else "SQLite"
        logger.info("Database connection verified (%s)", backend)
        return True
    except Exception:
        logger.exception("Cannot connect to the database")
        if IS_POSTGRES:
            logger.error("Check DATABASE_URL. For Supabase, use the "
                          "'Session pooler' string from Project Settings -> Database, "
                          "and make sure the password is URL-encoded.")
        return False


@contextmanager
def get_db_session():
    """Provide a transactional scope for database operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        # scoped_session binds the session to the current thread; remove it so
        # the next use in this thread starts clean.
        Session.remove()
