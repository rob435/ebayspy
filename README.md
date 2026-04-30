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
EBAY_APP_ID=your_ebay_client_id_optional_but_recommended
EBAY_CLIENT_SECRET=your_ebay_client_secret_optional_but_recommended
SELLERS=seller_one,seller_two
DESCRIPTION_CONCURRENCY=5
```

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

OAuth Browse API mode with `EBAY_APP_ID` and `EBAY_CLIENT_SECRET` is recommended. Without it,
ebayspy falls back to legacy eBay APIs and public eBay search pages, which can break if eBay
changes markup or rate-limits the VPS.

By default, the first scan seeds existing listings without alerting so you only get genuinely new
items after the tracker starts. Set `NOTIFY_EXISTING_ON_FIRST_RUN=true` if you want the current
latest listings sent immediately.
