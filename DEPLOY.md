# Deploying to Railway with a Supabase database

Everything runs as **one Railway service**: the bot (long polling) and the
CryptoBot webhook/healthcheck server share a single process, so they also share
one Supabase connection pool.

---

## 1. Supabase — create the database

1. Create a project at [supabase.com](https://supabase.com). Save the database
   password shown during setup — it is displayed only once.
2. Go to **Project Settings → Database → Connection string → URI**.
3. Pick the **Session pooler** string. It looks like:

   ```
   postgresql://postgres.abcdefghijklm:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
   ```

   > **Use the Session pooler, not the direct connection.** Supabase's direct
   > host resolves to IPv6 only, which Railway cannot reach on the default plan.
   > The session pooler is IPv4 and supports the row locks this bot relies on
   > for its purchase flow.

4. Replace `[YOUR-PASSWORD]` with your real password. **URL-encode it** if it
   contains any of `@ : / ? # & %`:

   | character | encode as |
   |---|---|
   | `@` | `%40` |
   | `:` | `%3A` |
   | `/` | `%2F` |
   | `#` | `%23` |
   | `?` | `%3F` |
   | `&` | `%26` |

You do **not** need to create any tables. The app calls `create_all()` on boot
and creates everything it needs on first run.

### Transaction pooler (port 6543)
Also supported — the app detects port `6543` and switches to `NullPool`
automatically. Prefer `5432` anyway: `SELECT ... FOR UPDATE` behaves more
predictably there, and that is what protects against double-spend.

---

## 2. Railway — deploy

1. Push this project to GitHub.
2. Railway → **New Project → Deploy from GitHub repo** → pick the repo.
3. Railway auto-detects Python via Nixpacks; `railway.json` supplies the start
   command and healthcheck. No Dockerfile needed.
4. Open **Variables** and add:

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | from @BotFather |
   | `ADMIN_TELEGRAM_ID` | your numeric Telegram ID (@userinfobot) |
   | `ADMIN_TELEGRAM_USERNAME` | your username, no `@` |
   | `BOT_USERNAME` | the bot's username, no `@` |
   | `DATABASE_URL` | the Supabase string from step 1 |
   | `CRYPTO_BOT_API_KEY` | optional, from @CryptoBot |
   | `TELEGRAM_PROVIDER_TOKEN` | optional, for card payments |

   Do **not** set `PORT` — Railway injects it.

5. **Settings → Networking → Generate Domain.** You need this for the webhook.
6. Deploy. A healthy boot logs:

   ```
   [OK] Configuration validated successfully
   [OK] Database connection verified (Supabase/PostgreSQL)
   [OK] Database tables created successfully
   Starting bot (long polling)...
   HTTP server thread started on port 8080
   ```

### Keep replicas at 1
`railway.json` pins `numReplicas: 1`. Two replicas would both long-poll the same
bot token, and Telegram would return
`Conflict: terminated by other getUpdates request` while updates get split
randomly between them.

---

## 3. Persistent storage for images (recommended)

Railway containers have an **ephemeral filesystem**: store logos and product
images uploaded through the admin panel are erased on every redeploy.

1. Railway → your service → **Volumes → New Volume**, mount path `/data`.
2. Add variable `ASSETS_DIR=/data/assets`.

The app creates the subdirectories on boot.

---

## 4. CryptoBot webhook (optional but recommended)

Without it, payments are only detected by the 30-second poller. With it,
confirmation is instant and the user is messaged right away.

1. Copy your Railway domain, e.g. `https://my-store.up.railway.app`.
2. @CryptoBot → **Crypto Pay → My Apps → your app → Webhooks → Enable**.
3. Set the URL to `https://my-store.up.railway.app/webhook/cryptobot`.

Signatures are verified with HMAC-SHA256 against your API token; requests
without a valid signature are rejected with 401.

---

## 5. Binance Pay (optional third top-up method)

The user sends USDT to your Binance Pay ID from the Binance app, then pastes
the Binance Transaction ID into the bot. Submitting that ID is what starts
verification — there is no separate "verify" button.

### Create the API key

1. Binance → **Account → API Management → Create API**.
2. Give it **read permission only**. Verification never moves funds, and a
   key that cannot withdraw or trade cannot drain the account if this server
   is ever compromised. Leave withdrawals, trading, futures and margin off.
3. **Whitelist your server's IP.** On Railway that means a static outbound
   IP; without one, an unrestricted key is the only thing that works, which
   is a materially worse security position.
4. Set `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_PAY_ID` and
   `BINANCE_PAY_ENABLED=true` in Railway's variables. Never put them in a
   file that gets committed.

The method stays hidden from users until it is enabled *and* configured, so
nobody can reach a checkout whose payment could never be verified.

### Try it without touching real money

`BINANCE_TEST_MODE=true` swaps in a mock provider that replaces only the
network call — the real verification rules still run. Submit `MOCK-SUCCESS-
25.00` against a 25.00 order to see a success, `MOCK-WRONG-AMOUNT` to see a
rejection; `services/binance_pay_mock.py` lists the rest. A mock payment can
only ever succeed while this flag is on. **Never leave it on in production**
— the bot logs a warning at every boot while it is.

### What this can and cannot do

Verification reads your account's Binance Pay history
(`GET /sapi/v1/pay/transactions`). That endpoint has real limits, and the
implementation works within them rather than around them:

- **No lookup by ID.** It returns recent history, which the bot searches for
  the submitted ID. History is capped, so a very old payment cannot be found.
- **No status field.** An incoming record with a positive amount in the
  expected asset is the documented success signal.
- **The destination is implicit** — the key's own account. Verification
  confirms money arrived in *your* account; it cannot separately assert
  which Pay ID was addressed.
- **It is expensive.** Weight(UID) 3000 per call, hence
  `BINANCE_VERIFY_RETRY_INTERVAL=180` rather than the 30-second CryptoBot
  poll, and a cap of 10 transactions per retry pass.

Because a mistyped ID and one that has not yet reached history look
identical, an unmatched ID is treated as "try again later", never as a
rejection. After `BINANCE_MAX_VERIFY_ATTEMPTS` the transaction moves to
**manual review** — it is never credited automatically, and never silently
expired. Review those under **Admin → 🟡 Binance Pay → Payment Monitoring**.

### Admin panel

**Admin → 🟡 Binance Pay** shows configuration status (credentials as
set/not-set plus the last four characters of the key — they are never
editable or displayed in Telegram), a connectivity test, and a kill switch
that stops new Binance top-ups without a redeploy. The switch can only
disable a working setup; it cannot enable the method on a server with no
credentials.

---

## 6. Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill it in
python app.py
```

Leave `DATABASE_URL` as the default to use a local SQLite file, or point it at
Supabase to work against the real schema.

To run the bot without the HTTP server: `WEBHOOK_ENABLED=false python app.py`
(or `python bot.py`).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not translate host name` | Wrong host, or you used the direct (IPv6) connection string. Switch to the Session pooler. |
| `password authentication failed` | Password not URL-encoded, or the `postgres.<ref>` username prefix was dropped. Copy the string from Supabase again. |
| `Conflict: terminated by other getUpdates` | The bot is running twice — check `numReplicas`, and stop any local instance. |
| Healthcheck fails | `WEBHOOK_ENABLED` is `false`, so nothing binds `$PORT`. Set it to `true` or remove the healthcheck from `railway.json`. |
| Images disappear after deploy | No volume attached. See section 3. |
| `remaining connection slots are reserved` | Lower `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, or close idle sessions in Supabase. |
| Tables missing | The user in `DATABASE_URL` lacks CREATE rights, or the boot log shows an earlier connection error. |

---

## Schema changes

Schema changes go through [Alembic](https://alembic.sqlalchemy.org/), not
`create_all()`. `create_all()` (run automatically at boot via `init_db()`)
only fills in tables that don't exist yet — it's a dev-only convenience for
a brand new empty database, and it silently does nothing to an existing
table when a column changes.

**Railway** runs migrations for you: `railway.json`'s `releaseCommand` runs
`alembic upgrade head` before every deploy's `startCommand`, against the
same `DATABASE_URL` the app uses.

**Local development:**

```bash
alembic upgrade head          # apply any migrations not yet applied
alembic revision --autogenerate -m "describe the change"   # after editing database/models.py
alembic downgrade -1          # undo the last migration
```

**Adopting Alembic on a database that already has tables** (created by the
old `create_all()`-only setup, before this project used Alembic): don't run
`alembic upgrade head` blind, or it will try to `CREATE TABLE` things that
already exist. Instead, tell Alembic the baseline is already applied:

```bash
alembic stamp 96e65c626176   # the baseline revision; see alembic/versions/
alembic upgrade head          # applies only what comes after the baseline
```

Everything schema-related now lives in `alembic/versions/`. An earlier
ad-hoc script (`migrations/categorynullable.py`) was removed: what it did
is already part of the Alembic baseline, and re-running it would have
rebuilt the `products` table with a `FLOAT` price column, undoing the
`Numeric(12,2)` money migration.
