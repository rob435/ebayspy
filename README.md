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
- Keeps a separate **observe list** for near-real-time new-listing alerts:
  - Observe-list sellers are polled on their own fast interval (default 3 minutes).
  - Each observe poll is a single cheap search; per-item detail is fetched only for
    genuinely new listings, so a tight cadence stays affordable.
  - The observe lane alerts on new listings only (no ended/restock alerts) and runs
    alongside the regular watch list.
- Lets you manage sellers from Telegram:
  - `/add sellername`
  - `/remove sellername`
  - `/list`
  - `/status`
  - `/check`
  - `/observe sellername [interval]` — start fast new-listing alerts (e.g. `/observe acme 90s`)
  - `/unobserve sellername`
  - `/observing` — show the observe list, intervals, and last check
  - `/interval sellername <time>` — change a seller's observe interval (e.g. `5m`, `1h`)
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

Manage the fast-poll observe list locally:

```powershell
ebayspy observe add sellername 3m
ebayspy observe list
ebayspy observe remove sellername
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

### API call budget and the observe list

The default Browse API quota is 5,000 calls/day; passing eBay's free Application Growth
Check raises it to roughly 1.5 million/day. The observe list is built for accounts with
the higher limit.

- A regular **watch** poll costs about `1 + MAX_ITEMS_PER_SELLER` calls per seller (one
  search plus one detail lookup per listing), because it also tracks ended and restock
  events.
- An **observe** poll costs about **1 call per seller** in steady state: just the search.
  Detail lookups happen only for items that are actually new.

So an observe-list seller uses roughly `86400 / interval` calls/day. At the 3-minute
default that is ~480 calls/day per seller; at 5 minutes, ~288. Budget your sellers and
intervals against your account's limit (set per seller with `/interval`, or globally with
`OBSERVE_INTERVAL_SECONDS`), and lower `OBSERVE_MIN_INTERVAL_SECONDS` only if you know your
quota can absorb it.

By default, the first scan seeds existing listings without alerting so you only get genuinely new
items after the tracker starts. Set `NOTIFY_EXISTING_ON_FIRST_RUN=true` if you want the current
latest listings sent immediately.
