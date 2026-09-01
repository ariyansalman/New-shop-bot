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
        pass


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
    from alembic.config import Config

    alembic_ini = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic.ini"
    )
    if not os.path.exists(alembic_ini):
        logger.warning("alembic.ini not found - skipping stamp of the new database")
        return

    cfg = Config(alembic_ini)
    # env.py reads the URL from settings, but set it explicitly so a stamp
    # can never land on a different database than the one just created.
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    # Keep Alembic's logging config out of this process: env.py's
    # fileConfig() would disable every logger the app already created (see
    # the comment there). Only the CLI should own logging configuration.
    cfg.attributes["configure_logger"] = False
    command.stamp(cfg, "head")
    logger.info("New database stamped at Alembic head")


def init_db():
    """Create any missing tables.

    Safe to run on every boot: create_all() only creates what does not exist.
    It does NOT alter existing tables - that's what the Alembic migrations in
    alembic/versions/ are for (run via `alembic upgrade head`, which Railway
    does automatically as a release step; see DEPLOY.md).

    When the database is completely empty, this also stamps it at Alembic
    head. Without that, `python app.py` on a fresh database (local dev, or
    a Docker run that skips the migration step) left an unstamped schema
    that every later `alembic upgrade head` failed against.
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
