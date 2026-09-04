"""Structural checks over the whole project.

Each one exists because that class of defect actually reached this
codebase: money credited without a row lock, blocking work on the event
loop, a nested session detaching the outer one's objects, and a security
helper copied until the copies disagreed. None of them are visible in a
diff, and none of them fail loudly at runtime.
"""

import ast
import pathlib
import sqlite3

from sqlalchemy import create_engine, inspect

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {"tests", "alembic", "__pycache__", ".git", ".venv"}


def production_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        yield rel, ast.parse(path.read_text())


def functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


# ------------------------------------------------------------ money

MONEY_COLUMNS = {"wallet_balance", "referral_earnings"}

# Stock is the same shape of defect with a different symptom. Restock
# recomputes stock_count from a count of unsold keys, order cancellation
# adds one back per returned key, and clearing keys writes zero - all
# read-modify-write against a row confirm_purchase locks while it sells.
# Lose the race and the store advertises keys it has already delivered.
STOCK_COLUMNS = {"stock_count"}


def _unlocked_writes(columns):
    """Writes to `columns` on a row whose query held no lock.

    Each function is judged on its own body. Assignments inside a nested
    function are skipped, because the handlers here are one async body
    wrapping several `def _sync()` closures: reading all of them together
    let one branch's unlocked query stand in for another branch's locked
    one, in either direction. The nested closures are checked in their own
    right, since functions() yields them too.
    """
    unlocked = []

    for rel, tree in production_files():
        for fn in functions(tree):
            nested = set()
            for node in ast.walk(fn):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node is not fn:
                    nested.update(id(sub) for sub in ast.walk(node))

            assigned = {}
            for node in ast.walk(fn):
                if id(node) in nested:
                    continue
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name):
                    assigned[node.targets[0].id] = ast.unparse(node.value)

            for node in ast.walk(fn):
                if id(node) in nested:
                    continue
                target = None
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and t.attr in columns:
                            target = t
                elif isinstance(node, ast.AugAssign) and \
                        isinstance(node.target, ast.Attribute) and \
                        node.target.attr in columns:
                    target = node.target
                if target is None or not isinstance(target.value, ast.Name):
                    continue

                source = assigned.get(target.value.id, "")
                if "with_for_update" not in source:
                    unlocked.append(
                        f"{rel}:{node.lineno} {fn.name} writes "
                        f"{target.value.id}.{target.attr}, but {target.value.id} "
                        f"was read without a lock")

    return unlocked


def test_every_stock_write_holds_a_row_lock():
    """Restock and a purchase touch the same row from two directions.

    Restock counts unsold keys and writes the total back; a purchase
    committing between the count and the write makes it restore the
    pre-sale figure, so the catalogue offers keys that are already gone.
    The buyer finds out at checkout.
    """
    unlocked = _unlocked_writes(STOCK_COLUMNS)

    assert not unlocked, (
        "Stock written on a row that was not locked:\n  "
        + "\n  ".join(unlocked))


def test_every_money_write_holds_a_row_lock():
    """A status check without a lock is a check-then-act race.

    Two concurrent runs - an admin double-tapping Confirm, or Telegram
    retrying a payment update - both read PENDING and both credit.

    The check follows the variable actually being written back to where it
    was assigned, rather than looking for with_for_update anywhere in the
    function: a function that locks one row and writes an unlocked second
    one is the exact shape this is meant to catch.
    """
    unlocked = []

    for rel, tree in production_files():
        for fn in functions(tree):
            # Where each local name was last assigned from a query.
            assigned = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name):
                    assigned[node.targets[0].id] = ast.unparse(node.value)

            for node in ast.walk(fn):
                target = None
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and t.attr in MONEY_COLUMNS:
                            target = t
                elif isinstance(node, ast.AugAssign) and \
                        isinstance(node.target, ast.Attribute) and \
                        node.target.attr in MONEY_COLUMNS:
                    target = node.target
                if target is None or not isinstance(target.value, ast.Name):
                    continue

                source = assigned.get(target.value.id, "")
                if "with_for_update" not in source:
                    unlocked.append(
                        f"{rel}:{node.lineno} {fn.name} writes "
                        f"{target.value.id}.{target.attr}, but {target.value.id} "
                        f"was read without a lock")

    assert not unlocked, (
        "Balance written on a row that was not locked:\n  "
        + "\n  ".join(unlocked))


# ------------------------------------------------------- event loop

def test_nothing_blocking_runs_on_the_event_loop():
    """One synchronous query stalls every other user's handler.

    The pattern this project uses is a nested sync closure handed to
    asyncio.to_thread; work directly in an async body is the defect.
    """
    offenders = []

    for rel, tree in production_files():
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            in_nested = set()
            for node in ast.walk(fn):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
                    in_nested.update(id(sub) for sub in ast.walk(node))

            for node in ast.walk(fn):
                if id(node) in in_nested:
                    continue
                name = None
                if isinstance(node, ast.With):
                    for item in node.items:
                        call = item.context_expr
                        if (isinstance(call, ast.Call)
                                and getattr(call.func, "id", None) in {"get_db_session", "open"}):
                            name = call.func.id
                elif (isinstance(node, ast.Call)
                      and getattr(node.func, "id", None) == "open"):
                    name = "open"
                if name:
                    offenders.append(f"{rel}:{node.lineno} {fn.name}() -> {name}()")

    assert not offenders, (
        "Blocking work in an async body, not in a to_thread closure:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_no_session_is_opened_inside_another():
    """get_db_session ends with Session.remove().

    The inner block's exit therefore detaches everything the outer block
    is still holding, and the next attribute read raises
    DetachedInstanceError - far from where the nesting was introduced.
    """
    nested = []

    for rel, tree in production_files():
        def walk(node, depth, rel=rel):
            opens = (isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", None) == "get_db_session"
                for i in node.items))
            if opens:
                if depth:
                    nested.append(f"{rel}:{node.lineno}")
                depth += 1
            for child in ast.iter_child_nodes(node):
                walk(child, depth)
        walk(tree, 0)

    assert not nested, "Nested get_db_session blocks:\n  " + "\n  ".join(nested)


# --------------------------------------------------------- security

def test_the_security_helpers_exist_once():
    """Authorization and secret redaction, copied, drift apart.

    The copy nobody remembers is the one that stays wrong.
    """
    duplicates = []

    for name in ("deny_if_not_admin", "mask_secret"):
        found = [f"{rel}:{fn.lineno}"
                 for rel, tree in production_files()
                 for fn in functions(tree) if fn.name == name]
        if len(found) > 1:
            duplicates.append(f"{name}: {found}")

    assert not duplicates, "\n  ".join(duplicates)


def test_no_secret_is_interpolated_into_a_string():
    """Anything shown to an admin goes through mask_secret first."""
    import re

    SECRETS = ("BOT_TOKEN", "BINANCE_API_KEY", "BINANCE_API_SECRET",
               "CRYPTO_BOT_API_KEY", "TELEGRAM_PROVIDER_TOKEN", "DATABASE_URL")
    leaks = []

    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "{" not in line:
                continue
            for secret in SECRETS:
                # A bare {settings.SECRET} inside a string, with no call
                # wrapping it, would put the value on screen.
                if re.search(r"\{\s*(app_)?settings\." + secret + r"\s*[}:]", line):
                    leaks.append(f"{rel}:{number}: {line.strip()}")

    assert not leaks, "Secrets interpolated directly:\n  " + "\n  ".join(leaks)


# -------------------------------------------------------- migrations

def test_the_migration_chain_has_one_head_and_one_base():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))

    assert len(script.get_heads()) == 1, script.get_heads()
    bases = [r.revision for r in script.walk_revisions() if r.down_revision is None]
    assert len(bases) == 1, bases


# SQLite is dynamically typed, so a VARCHAR(n) it built from an older enum
# still stores a longer member fine; on PostgreSQL the column is a native
# ENUM and migration c8d5f2a41b93 adds the values with ALTER TYPE. Recorded
# here so the check below stays useful rather than being deleted.
KNOWN_SQLITE_DRIFT = {("transactions", "status")}


def test_the_models_and_the_migrations_agree(tmp_path, monkeypatch):
    """create_all() and `alembic upgrade head` must build the same schema.

    They drift the moment a model column is added without a migration,
    and the only symptom is a production boot failing on a column that
    exists locally.
    """
    from alembic import command
    from alembic.config import Config
    import database.db as db
    from database.models import Base

    built = tmp_path / "models.db"
    migrated = tmp_path / "migrated.db"

    Base.metadata.create_all(create_engine(f"sqlite:///{built}"))

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{migrated}")
    cfg.attributes["configure_logger"] = False
    monkeypatch.setattr(db.settings, "DATABASE_URL", f"sqlite:///{migrated}",
                        raising=False)
    command.upgrade(cfg, "head")

    def schema(path):
        conn = sqlite3.connect(path)
        try:
            tables = [t for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'")]
            return {t: {r[1]: (r[2].upper(), bool(r[3]))
                        for r in conn.execute(f"PRAGMA table_info({t})")}
                    for t in tables}
        finally:
            conn.close()

    from_models, from_migrations = schema(built), schema(migrated)

    assert set(from_models) == set(from_migrations), (
        f"tables only in models: {sorted(set(from_models) - set(from_migrations))}, "
        f"only in migrations: {sorted(set(from_migrations) - set(from_models))}")

    drift = []
    for table in sorted(from_models):
        for column in sorted(set(from_models[table]) | set(from_migrations[table])):
            if (table, column) in KNOWN_SQLITE_DRIFT:
                continue
            a, b = from_models[table].get(column), from_migrations[table].get(column)
            if a != b:
                drift.append(f"{table}.{column}: models={a} migrations={b}")

    assert not drift, "Model/migration drift:\n  " + "\n  ".join(drift)


# The columns the busiest screens filter or sort on. Losing an index here
# costs nothing locally and turns into a full table scan per browse in
# production, which is exactly the kind of regression nobody notices.
HOT_PATH_INDEXES = {
    ("products", "category_id"),
    ("products", "subcategory_id"),
    ("products", "is_active"),
    ("orders", "status"),
    ("orders", "created_at"),
    ("orders", "user_id"),
    ("order_items", "order_id"),
    ("product_keys", "product_id"),
    ("product_keys", "is_sold"),
    ("product_keys", "order_id"),
    ("transactions", "user_id"),
    ("transactions", "status"),
    ("transactions", "created_at"),
    ("users", "telegram_id"),
    ("users", "referral_code"),
    ("users", "referred_by_id"),
}


def test_the_hot_read_paths_are_indexed(tmp_path, monkeypatch):
    """Every column above must carry an index in the migrated schema."""
    from alembic import command
    from alembic.config import Config
    import database.db as db

    migrated = tmp_path / "indexed.db"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{migrated}")
    cfg.attributes["configure_logger"] = False
    # alembic/env.py reads the URL from settings, not from the config.
    monkeypatch.setattr(db.settings, "DATABASE_URL", f"sqlite:///{migrated}",
                        raising=False)
    command.upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{migrated}"))
    indexed = set()
    for table in inspector.get_table_names():
        for index in inspector.get_indexes(table):
            for column in index["column_names"]:
                indexed.add((table, column))
        # A UNIQUE column is served by its own implicit index.
        for unique in inspector.get_unique_constraints(table):
            for column in unique["column_names"]:
                indexed.add((table, column))

    missing = sorted(HOT_PATH_INDEXES - indexed)
    assert not missing, f"unindexed hot-path columns: {missing}"


# ---------------------------------------------------------- wiring

def test_every_callback_button_reaches_a_handler():
    """A button whose callback nothing matches looks fine and does nothing."""
    import bot
    from telegram.ext import CallbackQueryHandler, ConversationHandler

    app = bot.build_application()

    patterns = []
    for handler in app.handlers[0]:
        if isinstance(handler, CallbackQueryHandler) and handler.pattern:
            patterns.append(handler.pattern)
        elif isinstance(handler, ConversationHandler):
            for group in (handler.entry_points, *handler.states.values(),
                          handler.fallbacks):
                for entry in group:
                    if isinstance(entry, CallbackQueryHandler) and entry.pattern:
                        patterns.append(entry.pattern)

    literals = set()
    for _rel, tree in production_files():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "InlineKeyboardButton"):
                for keyword in node.keywords:
                    if (keyword.arg == "callback_data"
                            and isinstance(keyword.value, ast.Constant)):
                        literals.add(keyword.value.value)

    unhandled = sorted(data for data in literals
                       if data != "noop"
                       and not any(p.match(data) for p in patterns))

    assert not unhandled, "Buttons nothing handles:\n  " + "\n  ".join(unhandled)
