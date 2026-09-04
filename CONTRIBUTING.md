# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env      # fill in BOT_TOKEN and ADMIN_TELEGRAM_ID at minimum
```

The default `DATABASE_URL` in `.env.example` is a local SQLite file - fine
for development. Point it at Supabase to work against the real schema.

## Running the bot locally

```bash
python app.py                        # bot + webhook/health server
WEBHOOK_ENABLED=false python app.py   # bot only, no HTTP server
python bot.py                        # equivalent to the line above
```

## Before you push

```bash
ruff check .          # lint
pytest tests/ -v      # test suite
```

Both run in CI (`.github/workflows/ci.yml`) on every push to `main` and
every pull request, along with a job that applies the Alembic migrations
against a real Postgres container and a job that builds the Dockerfile and
boots a container from it - so a change that only looks right on SQLite,
or only looks right without actually building the image, gets caught
before merge.

If you added a `pre-commit` hook locally (`pip install pre-commit &&
pre-commit install`), `ruff` runs automatically on `git commit`.

## Database schema changes

Don't edit `database/models.py` and rely on `create_all()` picking it up -
that only fills in tables that don't exist yet and never alters an
existing one. Use Alembic:

```bash
# after editing database/models.py:
alembic revision --autogenerate -m "describe the change"
# review the generated file in alembic/versions/ - autogenerate gets most
# things right but not always column defaults, server-side computed
# values, or (on Postgres) enum-type cleanup in downgrade()
alembic upgrade head
```

See `DEPLOY.md`'s "Schema changes" section for how this applies in
production (Railway runs migrations automatically as a release step).

## Tests

`tests/` uses pytest against a real (temp-file) SQLite database - see
`tests/conftest.py` for how that's wired up, and `tests/fakes.py` for the
minimal fake Telegram objects (`FakeQuery`, `FakeUpdate`, `FakeContext`,
...) handler-level tests are driven through. Prefer testing through the
actual handler function with these fakes over mocking internals - several
of the bugs this test suite guards against (a paid order with no keys, a
refund issued five times, a stock count silently doubling) only show up
when the whole handler runs end to end.

If you're adding a test for a new handler that isn't covered by the
existing fakes (e.g. one that reads `update.message.document` for a file
upload), extend `tests/fakes.py` rather than reaching for a mocking
library - keeping the fakes in one place is what makes them reusable.

## Code style

- `ruff` is the enforced linter (`pyproject.toml`'s `[tool.ruff]`); it's
  deliberately scoped to correctness/bug-risk checks (pyflakes,
  pycodestyle errors, flake8-bugbear) rather than full style enforcement,
  since the codebase predates linting.
- Money is `decimal.Decimal`, never `float`. Use `utils.money.to_money()`
  to round a computed value to cents (round-half-up, not Python's
  round-half-to-even `round()`), and `money_or_none()` when the input
  might not parse. See `utils/money.py`'s docstring for why.
- Logging, not `print()` - the app's runtime code (everything except the
  standalone `check_invoice.py` debug script)
  logs through the standard `logging` module so Railway's log level
  filtering applies uniformly, and so Sentry (when `SENTRY_DSN` is set -
  see `config/monitoring.py`) picks up every `logger.error()`/
  `logger.exception()` call automatically.
- In a Telegram callback-query handler, `query.answer()` may be called at
  most once. Authorize (`is_admin()`/ban check) *before* the first
  `answer()` call, not after - Telegram rejects a second `answer()` for
  the same callback query, so answering "success" before checking
  permissions means a denied user gets a false-positive popup and the
  real "Access denied" alert never arrives. If a handler needs to report a
  problem after its first unconditional `answer()`, use
  `edit_message_text()` for that, the way the rest of the admin handlers do.
- Admin actions that move money or affect another user's account (ban,
  refund, confirm/cancel a payment, resolve a dispute, restock, edit a
  price) should call `utils.log_admin_action()` inside the same
  `get_db_session()` block as the mutation, so the audit trail commits or
  rolls back together with it. See `handlers/admin_handlers.py` for
  examples.

## Repository layout

| Path | What |
|---|---|
| `bot.py`, `app.py` | Entry points - `app.py` also runs the CryptoBot webhook/health server |
| `handlers/` | Telegram command/callback/conversation handlers |
| `database/` | SQLAlchemy models (`models.py`) and session management (`db.py`) |
| `alembic/` | Schema migrations - see "Database schema changes" above |
| `services/crypto_bot.py` | CryptoBot API client |
| `utils/` | Shared helpers: `money.py`, `audit.py`, `keyboards.py`, `helpers.py` |
| `config/` | Settings (`settings.py`) and optional Sentry setup (`monitoring.py`) |
| `tests/` | pytest suite - see "Tests" above |

## Questions this doc doesn't answer

`AUDIT.md` has the history of what's been found and fixed and why (useful
context before touching payment or key-assignment code in particular).
`DEPLOY.md` covers Railway/Supabase deployment specifics.
