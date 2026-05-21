# ebayspy

Track specific eBay sellers from a VPS and receive Telegram notifications when they list new items.

## What it does

- Watches a persistent list of seller usernames.
- Polls every 15 minutes by default.
- Sends Telegram alerts with title, price, seller, description snippet, and item link.
- Includes listing type, category, and quantity available when eBay returns them.
- Alerts when eBay reports that an active listing's available quantity increased.
- Alerts when a previously active watched item disappears, which usually means ended or sold.
- Deduplicates items in SQLite so restarts do not resend old listings.
- Lets you manage sellers from Telegram:
  - `/add sellername`
  - `/remove sellername`
  - `/list`
  - `/status`
  - `/check`
  - `/help`
- Lets configured admins manage invite-only access from Telegram:
  - `/invite @username`
  - `/uninvite @username`
  - `/invites`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:your_bot_token
EBAY_APP_ID=your-ebay-app-id
EBAY_CLIENT_SECRET=your-ebay-cert-id
SELLERS=seller_one,seller_two
```

ebayspy reads seller listings from the official eBay Browse API. Register an
application at the [eBay Developers Program](https://developer.ebay.com/), then copy the
Production keyset's **App ID (Client ID)** and **Cert ID (Client Secret)** into `.env`.

Create a Telegram bot with BotFather, put its token in `.env`, then send `/start` to the bot.
If `TELEGRAM_CHAT_ID` is omitted, the app records chats that use `/start` and sends alerts there.
Set `TELEGRAM_ALLOWED_USERNAMES` or `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated allowlist
to make the bot invite-only.

## Run

```powershell
ebayspy run
```

For a one-off poll:

```powershell
ebayspy check
```

Manage sellers locally:

```powershell
ebayspy sellers add sellername
ebayspy sellers list
ebayspy sellers remove sellername
```

Check service health:

```powershell
ebayspy status
```

## VPS notes

Use `systemd/ebayspy.service` as a template. Update `WorkingDirectory`, `EnvironmentFile`,
and `ExecStart` for your server path.

ebayspy talks to the official eBay Browse API, so it runs reliably from a VPS with no
browser. The eBay API enforces per-application daily call limits; if you watch many
sellers, raise `POLL_INTERVAL_SECONDS` or lower `MAX_ITEMS_PER_SELLER` to stay within them.

By default, the first scan seeds existing listings without alerting so you only get genuinely new
items after the tracker starts. Set `NOTIFY_EXISTING_ON_FIRST_RUN=true` if you want the current
latest listings sent immediately.
