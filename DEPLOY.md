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

## 5. Local development

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

`create_all()` creates missing tables but never alters existing ones. To change
a column on a live database, run the SQL yourself in the Supabase SQL editor (or
adopt Alembic). See `migrations/categorynullable.py` for an example — it prints
the PostgreSQL equivalent when run against Supabase.
