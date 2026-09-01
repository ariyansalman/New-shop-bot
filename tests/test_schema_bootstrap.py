"""init_db() must leave any starting database at the latest revision.

These build real SQLite files in tmp_path and boot the real init_db()
against them. The case that matters most is the one that took production
down: a live database created by create_all() before Alembic existed, so
it has tables and data but no alembic_version row, and create_all() will
not add the columns newer code selects. Booting on that dies on the first
query with a raw driver error ("column settings.binance_pay_enabled does
not exist"), which is why the repair happens at boot rather than being
left to a platform release step that may silently not run.
"""

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

import database.db as db


HEAD = "d3f6b18c4a27"

# The shape of a real pre-Alembic deployment: no alembic_version, no
# users.language, no transactions.provider*, no settings.binance_pay_enabled,
# order_items.product_id still NOT NULL.
_LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL,
  username VARCHAR(255), wallet_balance NUMERIC(12,2) NOT NULL DEFAULT 0,
  is_banned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME);
CREATE TABLE settings (id INTEGER PRIMARY KEY, welcome_message TEXT,
  store_logo_path VARCHAR(500), support_username VARCHAR(255),
  channel_username VARCHAR(255), updated_at DATETIME);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  total_amount NUMERIC(12,2) NOT NULL, status VARCHAR(20),
  dispute_status VARCHAR(20), created_at DATETIME, completed_at DATETIME);
CREATE TABLE products (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL,
  description TEXT, price NUMERIC(12,2) NOT NULL, stock_count INTEGER,
  product_type VARCHAR(20) NOT NULL, category_id INTEGER, subcategory_id INTEGER,
  image_path VARCHAR(500), download_link VARCHAR(500), is_active BOOLEAN,
  created_at DATETIME);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL, quantity INTEGER NOT NULL,
  price NUMERIC(12,2) NOT NULL, delivered_asset TEXT, created_at DATETIME);
CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  amount NUMERIC(12,2) NOT NULL, payment_method VARCHAR(20) NOT NULL,
  crypto_address VARCHAR(500), status VARCHAR(20), created_at DATETIME,
  expires_at DATETIME, completed_at DATETIME);
CREATE TABLE admin_action_logs (id INTEGER PRIMARY KEY,
  admin_telegram_id BIGINT NOT NULL, action VARCHAR(100) NOT NULL,
  target_type VARCHAR(50), target_id INTEGER, details TEXT, created_at DATETIME);
INSERT INTO settings (id, welcome_message) VALUES (1, 'existing store');
INSERT INTO users (id, telegram_id, wallet_balance) VALUES (1, 555, 42.50);
"""


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """Point init_db() at a throwaway SQLite file and run it."""
    def _boot(filename: str, schema_sql: str = None, create_all_first: bool = False):
        path = tmp_path / filename
        url = f"sqlite:///{path}"

        if schema_sql:
            conn = sqlite3.connect(path)
            conn.executescript(schema_sql)
            conn.commit()
            conn.close()

        engine = create_engine(url)
        monkeypatch.setattr(db, "engine", engine)
        monkeypatch.setattr(db.settings, "DATABASE_URL", url, raising=False)

        if create_all_first:
            db.Base.metadata.create_all(engine)

        db.init_db()
        return path, engine

    return _boot


def version_of(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def columns(engine, table) -> set:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_legacy_database_is_detected_and_migrated_to_head(boot):
    """The production failure: tables and data, no alembic_version."""
    path, engine = boot("legacy.db", _LEGACY_SCHEMA)

    assert version_of(path) == HEAD
    # Every column newer code selects is now present.
    assert "binance_pay_enabled" in columns(engine, "settings")
    assert "language" in columns(engine, "users")
    assert "provider" in columns(engine, "transactions")
    assert "verification_attempts" in columns(engine, "transactions")


def test_legacy_migration_keeps_the_existing_data(boot):
    """A schema repair must never cost a user their balance."""
    path, _engine = boot("legacy_data.db", _LEGACY_SCHEMA)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT wallet_balance FROM users").fetchone()[0] == 42.5
        row = conn.execute(
            "SELECT welcome_message, binance_pay_enabled FROM settings").fetchone()
        assert row[0] == "existing store"     # the store's own text survives
        assert row[1] == 1                    # new column defaults to enabled
    finally:
        conn.close()


def test_booting_twice_on_a_legacy_database_is_a_no_op(boot):
    path, _engine = boot("legacy_twice.db", _LEGACY_SCHEMA)
    assert version_of(path) == HEAD

    db.init_db()          # same engine, second boot
    assert version_of(path) == HEAD


_FOREIGN_VERSION_TABLE = """
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
INSERT INTO alembic_version VALUES ('0005');
"""


def test_database_stamped_by_a_different_alembic_setup_is_recovered(boot):
    """The second production failure: alembic_version says '0005'.

    A database previously managed by another Alembic setup, one that
    numbered revisions 0001..0005. Alembic cannot upgrade from a revision
    it cannot resolve ("Can't locate revision identified by '0005'"), so
    the row is replaced with the revision the schema is really at.
    """
    path, engine = boot("foreign.db", _LEGACY_SCHEMA + _FOREIGN_VERSION_TABLE)

    assert version_of(path) == HEAD
    assert "binance_pay_enabled" in columns(engine, "settings")
    assert "provider" in columns(engine, "transactions")


def test_recovering_a_foreign_stamp_leaves_exactly_one_version_row(boot):
    """Two rows would read as a branched head and break the next upgrade."""
    path, _engine = boot("foreign_single.db", _LEGACY_SCHEMA + _FOREIGN_VERSION_TABLE)

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert rows == [(HEAD,)]


def test_recovering_a_foreign_stamp_keeps_the_existing_data(boot):
    path, _engine = boot("foreign_data.db", _LEGACY_SCHEMA + _FOREIGN_VERSION_TABLE)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT wallet_balance FROM users").fetchone()[0] == 42.5
        assert conn.execute(
            "SELECT welcome_message FROM settings").fetchone()[0] == "existing store"
    finally:
        conn.close()


def test_a_valid_stamp_is_left_alone(boot):
    """A correctly stamped database must not be re-detected and re-stamped."""
    path, _engine = boot("valid_stamp.db")      # fresh -> stamped at head
    assert version_of(path) == HEAD

    db.init_db()                                 # boots again, nothing to do
    assert version_of(path) == HEAD


def test_fresh_database_is_stamped_not_migrated(boot):
    """create_all() already built head - running migrations over it fails."""
    path, engine = boot("fresh.db")

    assert version_of(path) == HEAD
    assert "binance_pay_enabled" in columns(engine, "settings")


def test_create_all_built_database_without_a_version_row(boot):
    """Current schema, no alembic_version: stamp head, apply nothing.

    Re-running the Binance migrations here would try to add columns that
    create_all() already made, so this proves the probes place it at head.
    """
    path, engine = boot("current.db", create_all_first=True)

    assert version_of(path) == HEAD
    assert "provider" in columns(engine, "transactions")


def test_partially_migrated_database_gets_only_what_it_is_missing(boot):
    """A database stopped halfway resumes from where it actually is."""
    partial = _LEGACY_SCHEMA.replace(
        "is_banned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME);",
        "is_banned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME, "
        "language VARCHAR(10) NOT NULL DEFAULT 'en');",
    )
    path, engine = boot("partial.db", partial)

    assert version_of(path) == HEAD
    assert "binance_pay_enabled" in columns(engine, "settings")
    assert "provider" in columns(engine, "transactions")


def test_boot_fails_loudly_rather_than_running_on_a_broken_schema(boot, monkeypatch):
    """Serving on a schema the queries do not match is worse than stopping."""
    def _explode(*_args, **_kwargs):
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(db, "_sync_schema", _explode)

    with pytest.raises(RuntimeError, match="migration blew up"):
        boot("broken.db", _LEGACY_SCHEMA)
