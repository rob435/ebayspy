# ebayspy

Track specific eBay sellers from a VPS and receive Telegram notifications when they list new items.

## What it does

- Watches a persistent list of seller usernames.
- Polls every 15 minutes by default.
- Sends Telegram alerts with title, price, seller, description snippet, and item link.
- Shows the seller as a clickable profile link with their eBay rating, e.g. `98.6% (1004)`
  (positive-feedback percentage and total feedback count), plus the listed time, on every
  alert — new, restock, ended/sold, and market deals.
- Includes listing type, category, and quantity available when eBay returns them.
- Alerts when eBay reports that an active listing's available quantity increased.
- Alerts when a previously active watched item disappears, which usually means ended or sold.
- Deduplicates items in SQLite so restarts do not resend old listings.
- Keeps a separate **market watch** lane that snipes below-market deals:
  - You name an item (search terms, optionally a condition and a price cap); ebayspy
    samples the live Buy It Now listings and derives a **market price** from the trimmed
    median total cost (item + shipping).
  - When a listing appears at or below your discount threshold (default 15% under market),
    it sends a deal alert showing the price, the market estimate, and your saving.
  - **Smart comparable matching** is the key to a correct market price. A keyword search
    returns the item *plus* accessories, spare parts, wall brackets, wrong variants, faulty
    units, and multi-item lots — pricing over that mix is meaningless. ebayspy narrows the
    sample to genuinely comparable listings with a **layered hybrid** engine:
    - **Guardrail rules** (always enforced): identity discriminators — model numbers and
      salient digits (`hu02`, and the `13` in *iPhone 13*) are pinned exactly, so iPhone 13 ≠
      14 and PS4 ≠ PS5; accessory / parts / lot / damage vocabulary is rejected; pinned
      attributes (capacity/colour/model-line) must match.
    - **Relevance** then passes on any of token coverage, **fuzzy ratio** (word order / typos,
      via rapidfuzz), or **semantic similarity** (static embeddings — install the `nlp` extra),
      so the same product described in very different words matches, across *any* category.
    - **eBay structured data**: comparable listings are enriched with eBay's own catalog
      aspects (ePID, GTIN, MPN, Storage Capacity, Colour, …) via `getItem`, so variant
      clustering uses authoritative values instead of parsing titles. (Bounded by
      `MARKET_HYDRATE_LIMIT` to control API cost; can be turned off.)
    The `/watches` list shows how many comparable listings backed each price (e.g.
    `market ≈ 412.50 (28 comparable/200)`) so you can tighten a watch that matches too few.
  - **Per-variant pricing.** Different colours, capacities, and model lines sell for very
    different prices, so blending them into one median is misleading. ebayspy detects the
    variant of each listing (capacity like `256GB`, colour, and model line like
    `pro`/`max`/`mini`) and:
    - **prices each variant against its own kind.** For any attribute you leave open, ebayspy
      groups the comparable listings by the *combination* of attributes that genuinely move
      the price (e.g. model line **and** capacity together) and prices each group separately
      — so a plain `iphone 13` watch surfaces the base, Pro, and Pro Max at each capacity as
      their own markets. A Pro Max 256GB is judged against the Pro Max 256GB market, never a
      blended one, so real deals on the pricier variants are caught and cheap small variants
      are not mistaken for deals. It only splits on an attribute when the data supports it
      (enough listings, prices actually differ), so it never fragments on, say, a colour that
      doesn't affect price.
    - **respects what you pin.** `/watch iphone 13 256gb blue` requires 256GB and blue;
      `/watch iphone 13 pro` matches only the Pro (not the plain 13 or the Pro Max).
    Deal alerts state the variant the comparison used (`Market ≈ £722 for pro max · 256gb`),
    and `/watches` tags the headline price with the dominant variant.
  - A floor (default 40% of market) suppresses "too good to be true" listings that are
    almost always the wrong item or a scam; a minimum comparable-sample gate refuses to
    price (or alert) until enough comparables back the median; and alerts are capped per
    cycle so a backlog never floods the chat.
  - You can refine matching per watch: add `exclude:tap` (or `-tap`) to drop unwanted
    variants, `condition:new|used`, and `under:PRICE` to bound the sample.
  - **Real sold prices (optional).** By default the market figure is a live-asking estimate
    (the Browse API doesn't expose sold prices). If you have approved access to eBay's
    **Marketplace Insights API** (a Limited Release), set `MARKET_PRICE_SOURCE=insights` and
    each variant is priced against its *actual sold median*, and demand becomes a measured
    sales-velocity instead of the disappearance proxy. It falls back to the live estimate
    automatically if the call isn't entitled.
  - **Rich, actionable deal alerts.** Each deal shows the item photo, an estimated **flip
    profit & ROI** (market minus buy price minus configurable eBay fees), a **price trend**
    (e.g. `Market -8% vs 7d`), a **demand/liquidity read**, the **price spread** (P10–P90)
    and **recent sold comps** (with Insights), a best-offer flag, the seller rating/link, and
    inline buttons to open the listing, **mute that variant**, or flag a **wrong match**
    (which teaches the watch a new exclude term).
  - **Sharper sourcing.** Surfaces **no-bid / low-competition auctions** at a relaxed
    threshold (win uncontested); flags **scam/risk** (new seller, price too-good, foreign
    location) and can suppress the worst; spots **lot/bundle arbitrage** (`/watch … lots:on`)
    by valuing a job-lot of N at N× the single-unit market; and — with the optional `vision`
    extra — **verifies the listing photo** matches the product and catches mistitled/
    misgraded gems (tagged "for parts" but the photo looks new).
  - **Auction sniping** (`/watch … auctions:on`): auctions are priced off their live bid
    against the fixed-price market, with a heads-up when a bid drops below market and a
    second **final-call** alert as the auction enters its closing window — the watch
    automatically switches to a tight "turbo" poll so the final call lands in time.
  - **Demand intelligence.** ebayspy records each comparable listing's lifecycle over time
    (first/last seen, disappearance, price cuts) and infers a liquidity read — how fast the
    item clears, median time-to-sell, age of standing stock, and discount pressure — hedged
    with a confidence gate so it stays "warming up" until enough data accrues. See it any
    time with `/demand <id>`. (Disappearance ≈ sold/pulled; it is a signal, not a sale feed.)
  - **Cross-marketplace arbitrage** (`/watch … markets:GB,DE,US`): prices the same item on
    each marketplace, converts via live FX, and alerts a buy-low/sell-high gap (before fees,
    shipping, import duty — flagged as such).
  - **Synonyms** (`ps5` ⇄ `playstation 5`), **category pinning** (`category:NNN` to cut
    junk), **watch-health** alerts when a watch starts erroring or finding nothing, and
    **per-user ownership** so a watch a user adds alerts that user (env/CLI watches still
    broadcast).
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
  - `/watch <terms> [condition:new|used] [under:PRICE] [discount:%] [every:TIME] [exclude:word] [category:ID] [auctions:on] [markets:GB,DE,US]` —
    snipe below-market deals (e.g. `/watch dyson airblade hu02 condition:new under:400 discount:20`)
  - `/unwatch <id>` — stop a market watch (ids shown by `/watches`)
  - `/watches` — list market watches with market estimate, variant, price trend, and last check
  - `/demand <id>` — the demand/liquidity read for a watch
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

Manage market deal-watches locally:

```powershell
ebayspy market add "dyson airblade hu02" --condition new --under 400 --discount 20
ebayspy market list
ebayspy market remove 1
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
- A **market watch** costs about **1 call per watch per cycle**: a single search that both
  prices the market and surfaces deals (no per-item detail lookups). At the 10-minute
  default that is ~144 calls/day per watch; tune with `MARKET_INTERVAL_SECONDS` or per
  watch via `/watch ... every:TIME`.

So an observe-list seller uses roughly `86400 / interval` calls/day. At the 3-minute
default that is ~480 calls/day per seller; at 5 minutes, ~288. Budget your sellers and
intervals against your account's limit (set per seller with `/interval`, or globally with
`OBSERVE_INTERVAL_SECONDS`), and lower `OBSERVE_MIN_INTERVAL_SECONDS` only if you know your
quota can absorb it.

By default, the first scan seeds existing listings without alerting so you only get genuinely new
items after the tracker starts. Set `NOTIFY_EXISTING_ON_FIRST_RUN=true` if you want the current
latest listings sent immediately.
