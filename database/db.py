"""Database connection and session management.

Supports both SQLite (local development) and PostgreSQL/Supabase (production).
The engine is configured differently for each: SQLite needs pragmas and
thread-safety flags, PostgreSQL needs pool sizing and keepalives.
"""

import logging

from sqlalchemy import create_engine, event
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


def init_db():
    """Create any missing tables.

    Safe to run on every boot: create_all() only creates what does not exist.
    It does NOT alter existing tables, so a schema change still needs a real
    migration (see migrations/).
    """
    Base.metadata.create_all(engine)
    print("[OK] Database tables created successfully")


def check_connection() -> bool:
    """Verify the database is reachable, with a readable error if it is not."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        backend = "Supabase/PostgreSQL" if IS_POSTGRES else "SQLite"
        print(f"[OK] Database connection verified ({backend})")
        return True
    except Exception as e:
        print(f"[ERROR] Cannot connect to the database: {e}")
        if IS_POSTGRES:
            print("        Check DATABASE_URL. For Supabase, use the "
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
