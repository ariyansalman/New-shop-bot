"""Alembic environment.

Pulls the database URL and the ORM metadata straight from the app's own
config/models instead of a separately-maintained sqlalchemy.url in
alembic.ini, so there's exactly one place (config/settings.py) that knows
how to build a connection string.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the app package importable when Alembic is run from the repo root
# (the usual case) or from anywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from database.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate support: compare Base.metadata against the live schema.
target_metadata = Base.metadata

# Always use the app's own DATABASE_URL (handles the postgres:// ->
# postgresql+psycopg2:// rewrite and the Supabase sslmode default), rather
# than whatever static value sits in alembic.ini.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL, no DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER COLUMN in place; batch mode rebuilds the
            # table under the hood instead. Postgres ignores this and runs
            # the plain ALTER TABLE, so the same migration script works on
            # both a local SQLite dev DB and production Postgres/Supabase.
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
