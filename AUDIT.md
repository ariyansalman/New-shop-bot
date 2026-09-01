# Static Audit — Free Telegram Store Bot

Every item below was found by reading the code (no runtime environment was
available: the container has no network access, and neither `python-telegram-bot`
nor `SQLAlchemy` is installed). Findings are ordered by severity.

---

## 0. Follow-up audit (2026-09-01)

The fixes below were already in place from the previous pass (verified by
re-reading the code, not assumed). Two new issues turned up on a fresh read:

### 0.1 CryptoBot webhook and the 30s poller could double-credit the same payment
`webhook_server.py :: process_invoice_paid`, `handlers/payment_handlers.py ::
check_pending_payments`

Both paths query for transactions with `status == PENDING` and then set them
to `COMPLETED` and credit the wallet, but neither locked the row first. The
webhook (Flask thread) and the poller (asyncio job, every 30s) run in the same
process, so if a payment confirmation arrived from both channels close
together, both could read `PENDING` before either committed and credit the
wallet twice for one payment.

**Fixed:** both paths now re-fetch the specific transaction with
`with_for_update()` and re-check `status == PENDING` immediately before
crediting; PostgreSQL re-evaluates the `WHERE` clause after the lock is
granted, so whichever path loses the race sees the row already `COMPLETED`
and skips it. (No-op on SQLite, same caveat as the existing purchase-flow
locks — see "SQLite + `with_for_update()`" below.)

### 0.2 Dispute resolution answered the same callback query twice
`handlers/dispute_handlers.py :: admin_resolve_dispute_callback`

`query.answer()` fired unconditionally after the admin check, and then
`query.answer("⚠️ This dispute is already resolved.", ...)` fired again when
the dispute was already resolved — the exact "answer twice" bug described in
1.2 below, just not caught in that pass. Clicking resolve on an
already-resolved dispute raised instead of showing the intended alert.

**Fixed:** the already-resolved case now uses `edit_message_text`, matching
the idiom the other admin handlers already use for this.

---

## 1. Security

### 1.1 Dispute admin panel had no authorization at all — **critical**
`handlers/dispute_handlers.py`

`admin_view_disputes_callback`, `admin_dispute_detail_callback` and
`admin_resolve_dispute_callback` were registered as plain callback handlers in
`bot.py` and never called `is_admin()`. Callback data is client-supplied, so any
user could send `admin_view_disputes` and read every dispute in the store —
including other customers' Telegram IDs, usernames, order totals and purchased
items — and then close disputes with `resolve_dispute_<id>`.

**Fixed:** all three handlers now authorize before doing anything, and resolving
an already-resolved dispute is rejected.

### 1.2 Ban/unban confirmed success to unauthorized users
`handlers/admin_handlers.py`

```python
await query.answer("✅ User banned successfully!", show_alert=True)   # fired first
if not is_admin(...):                                                # checked second
```
A non-admin got a "User banned successfully!" popup, and the subsequent
`answer("Access denied")` raised because Telegram rejects a second answer for the
same query. The same answer-then-authorize ordering existed in **27 other admin
handlers**, so the "Access denied" alert never actually reached anyone.

**Fixed:** authorization now runs before `query.answer()` everywhere.

### 1.3 Fabricated payment invoices when CryptoBot was unconfigured
`services/crypto_bot.py`

With no `CRYPTO_BOT_API_KEY`, `generate_payment_address()` returned
`f"{transaction_id}|https://t.me/CryptoBot?start=sample_invoice_..."`. The bot
then showed a real-looking "Pay with Any Crypto" button for an invoice that did
not exist and could never be verified.

**Fixed:** returns `None`, the top-up fails cleanly, and `validate_settings()`
warns at startup which payment methods are disabled.

### 1.4 Paid amount was never verified
`services/crypto_bot.py`, `webhook_server.py`

The wallet was credited with `transaction.amount` whenever the invoice looked
paid; `expected_amount` was accepted as a parameter and then ignored.

**Fixed:** both the polling path and the webhook compare the invoice amount
against the pending transaction before crediting.

### 1.5 Webhook signature check ran against an empty secret
`webhook_server.py`

`hashlib.sha256(settings.CRYPTO_BOT_API_KEY.encode())` with an unset key derives
a *known* secret, so an attacker could forge valid signatures. The endpoint also
dumped the full webhook payload to stdout and returned internal exception text
to the caller.

**Fixed:** unconfigured token → reject; payload logging removed; generic 500.

---

## 2. Money-losing logic bugs

### 2.1 Purchases could create a paid order with no keys — **critical**
`handlers/payment_handlers.py :: confirm_purchase`

The order was `session.add()`-ed **and committed** before keys were assigned.
`assign_product_keys()` then raised `ValueError` if inventory did not back the
advertised `stock_count`. Result: a committed, COMPLETED order with no keys, no
wallet deduction, and an unhandled exception shown to nobody.

**Fixed:** keys are reserved first, and order creation, key assignment, stock
decrement and wallet debit all happen in one transaction that rolls back
together. `session.flush()` is used instead of an early `commit()`.

### 2.2 Double-spend on rapid taps
Same handler. Balance was read, then written, with no row lock — two quick taps
on "Confirm Purchase" could both pass the balance check. Quantity also came
straight from client-supplied callback data with no upper bound.

**Fixed:** `with_for_update()` on the user and product rows, plus a
`MAX_PURCHASE_QUANTITY` ceiling.

### 2.3 "Cancel Order" refunded on every click
`handlers/admin_handlers.py :: admin_cancel_order_callback`

No check on the current status, so clicking the button five times credited the
order total five times. Assigned keys were also never returned to stock.

**Fixed:** already-cancelled orders are rejected, and sold keys are released back
to inventory with the stock count restored.

### 2.4 `Total Spent` was always $0.00
`filter_by(status='completed')` — SQLAlchemy persists `Enum` members by **name**
(`"COMPLETED"`), so the string never matched. Three occurrences.

**Fixed:** compares against `OrderStatus.COMPLETED`.

### 2.5 The CryptoBot webhook could never find its transaction
`webhook_server.py`

```python
Transaction.payment_method.in_(['crypto_wallet'])   # stored value is 'CRYPTO_WALLET'
```
Every webhook logged "No pending transaction found" and was dropped, silently
degrading the whole integration to 30-second polling.

**Fixed:** compares against `PaymentMethod.CRYPTO_WALLET`.

### 2.6 Duplicate keys inflated stock
Restock and product creation inserted pasted keys verbatim, so a re-uploaded file
doubled `stock_count` and the same key could be delivered to two customers.

**Fixed:** de-duplication on both paths, a `UniqueConstraint` on
`(product_id, key_value)`, and stock recomputed from the real unsold-key count.

---

## 3. Broken / dead functionality

| Problem | Location | Fix |
|---|---|---|
| `requirements.txt` listed `telebot==0.0.5` — the code imports `telegram.ext` (**python-telegram-bot**). `SQLAlchemy` and `requests` were missing entirely, and `job_queue` needs an extra. The project could not be installed as specified. | `requirements.txt` | Rewritten with the actual dependencies |
| Product-list paging emitted `products_page_N`, which routed to the **category** list handler; the page number never reached `show_products_list`, so paging was completely broken | `user_handlers.py` | New `plist_<scope>_<id>_<page>` prefix + dedicated handler |
| Subcategory list hard-capped at `[:5]` with no paging — extra subcategories unreachable | `user_handlers.py` | Cap removed |
| "🔄 Reactivate Order" button had no registered handler | `admin_handlers.py`, `bot.py` | Handler added |
| "Page x/y" labels used `callback_data="noop"` with no handler → client spinner never cleared | `bot.py` | `noop_callback` added |
| `query.answer()` called twice in complete-order / confirm-payment / cancel-payment / cancel-order (the second call raises) | `admin_handlers.py` | Split into `_render_*` helpers |
| No `add_error_handler` — every handler exception vanished silently | `bot.py` | Global error handler added |
| Availability broadcast ran `first=10`, so **every restart** blasted the entire user base | `bot.py` | `first=43200` |
| Broadcasts were sent to banned users | `admin_conversations.py` | Filtered on `is_banned=False` |
| `get_or_create_user()` returned a detached ORM object → `DetachedInstanceError` on any later attribute access | `utils/helpers.py` | Returns a plain dict |
| `SETTING_VALUE = range(1)` used as a conversation state key | `admin_conversations.py` | `= 0` |
| `prompt` referenced before assignment for unrecognized fields (3 places) | `admin_conversations.py` | `else` branches added |
| Non-UTF-8 key file → `UnicodeDecodeError` crash; no size limit | `admin_handlers.py`, `admin_conversations.py` | `errors='replace'`, 1 MB cap |
| Order views crashed on `item.product.name` when a product had been deleted | `user_handlers.py`, `dispute_handlers.py` | Null-guarded |
| `parse_mode='Markdown'` on text containing `#`, `$`, `_` → Telegram rejects the message | `payment_handlers.py` | Removed |
| Pending-payment page could build an `InlineKeyboardButton(url="#")` → `BadRequest` | `payment_handlers.py` | Validated, graceful message |
| `paid_btn_url` hardcoded to `https://t.me/your_bot` | `crypto_bot.py` | New `BOT_USERNAME` setting; omitted when unset |
| Username captured once at signup, never refreshed | `user_handlers.py` | Updated on `/start` |
| Card top-ups were polled by the crypto checker every 30s for nothing | `payment_handlers.py` | Query filtered to crypto |
| Seven unregistered duplicate handlers (`handle_broadcast_text`, `handle_ban_user`, …) shadowing the real conversation flows | `admin_handlers.py` | Removed |

---

## 4. Data model & infrastructure

- **`telegram_id` was `Integer`.** Telegram user IDs already exceed the signed
  32-bit range; this works on SQLite (dynamic typing) but overflows on
  PostgreSQL/MySQL. → `BigInteger`.
- **`declarative_base` imported from the deprecated `sqlalchemy.ext.declarative`.**
  → `sqlalchemy.orm`.
- **SQLite foreign keys were never enforced** (off by default). → `PRAGMA
  foreign_keys=ON`, plus WAL mode, since `bot.py` and `webhook_server.py` write
  to the same file.
- **`check_same_thread`** — payment jobs touch the DB from `asyncio.to_thread`
  worker threads. → `connect_args` set, `scoped_session` properly removed after
  each request.
- **Ban cache was mutated from multiple threads** with no lock and no size bound.
  → Lock added, capped at 10k entries.
- **`validate_amount`** accepted `"nan"` and `"inf"` (both parse as floats) and
  hardcoded its limits. → Finite check, configurable min/max.
- **`.env` with credentials was committed** (`.gitignore` lists it, but the file
  is in the archive). → Removed; `.env.example` extended with the new keys.

---

## Remaining recommendations (not changed)

1. **SQLite + `with_for_update()`.** Row locks are a no-op on SQLite; the new
   locking only takes effect on PostgreSQL/MySQL. For real concurrency, move the
   database.
2. **`stock_count` is denormalized** from `product_keys`. It is now recomputed on
   restock and repaired on shortage, but a periodic reconciliation job would be
   safer.

Resolved since the first pass, as part of the "make this more professional"
work (see git log / commit messages for detail on each):
- **Money as `Float`** -> `Numeric(12, 2)` columns + `decimal.Decimal`
  arithmetic throughout (`utils/money.py`), via an Alembic migration.
- **`webhook_server.py` runs Flask's dev server** -> already moved to
  waitress, and the webhook does have a bot instance to notify through
  (`app.py`'s `_make_threadsafe_notifier`); this item was stale.

"No tests" is being worked on next in the same pass (see the project's task
list / commit history for current status).
