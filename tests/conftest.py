"""Test setup.

Sets the environment BEFORE anything in the app is imported (config, engine,
etc. are all built at import time from env vars), then gives every test a
clean schema on a throwaway SQLite file.

This deliberately does not use `sqlite:///:memory:` - the app's engine is a
module-level singleton created once at import time, and getting an in-memory
DB to behave the same way across the multiple connections a real test run
opens (via get_db_session()) requires pool tuning the app itself doesn't use.
A real temp file behaves exactly like the SQLite dev database the app is
already designed to run against, with none of that risk.
"""

import os
import tempfile

_fd, _TEST_DB_PATH = tempfile.mkstemp(prefix="shopbot_test_", suffix=".db")
os.close(_fd)

os.environ["BOT_TOKEN"] = "test-token"
os.environ["ADMIN_TELEGRAM_ID"] = "1000000001"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ["WEBHOOK_ENABLED"] = "false"
os.environ.setdefault("CRYPTO_BOT_API_KEY", "")
os.environ.setdefault("TELEGRAM_PROVIDER_TOKEN", "")

import atexit  # noqa: E402
import pytest  # noqa: E402

from database.db import engine, Session  # noqa: E402
from database.models import Base  # noqa: E402


@atexit.register
def _cleanup_test_db():
    try:
        os.remove(_TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh, empty schema for every single test - no state leaks between tests."""
    Session.remove()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Session.remove()
