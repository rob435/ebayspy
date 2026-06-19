# ebayspy

Track specific eBay sellers from a VPS and receive Telegram notifications when they list new items.

## What it does

- Watches a persistent list of seller usernames.
- Polls every 15 minutes by default.
- Sends Telegram alerts with title, price, seller, description snippet, and item link.
- Shows the seller as a clickable profile link with their eBay rating, e.g. `98.6% (1004)`
  (positive-feedback percentage and total feedback count), plus the listed time, on every
  alert — new, restock, price drop, ended/sold, and market deals.
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
      14 and PS4 ≠ PS5; edition designators are pinned in both directions, so Xbox Series X ≠
      Series S, Canon R6 ≠ R6 Mark II, and Sony A7 III ≠ A7 II; accessory / parts / lot /
      damage vocabulary is rejected; pinned attributes (capacity/colour/model-line) must
      match. Written-form quirks are read the way a human would — `WH-1000XM5` ≡ `WH1000XM5`,
      `S21+` ≡ `S21 Plus`, `AirPods Pro 2nd Gen` ≡ `2` — so a punctuation choice never drops
      a genuine comparable. Niche identity also pins when you name it: a graded-card grade
      (`PSA 10` ≠ `PSA 9`), a fragrance concentration (`EDP` ≠ `EDT`), a liquid volume
      (`100ml` ≠ `60ml`), a watch reference (`116610` ≡ `116610LN`) and a lens aperture
      (`f/1.8` ≡ `f1.8`).
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
    inline buttons to open the listing, **mute that variant**, flag a **wrong match**
    (which teaches the watch a new exclude term), or rate the deal **👍/👎** (which tunes the
    watch's required discount over time).
  - **Best-offer candidates** (`MARKET_OFFER_AWARE=true`). Flags Best-Offer listings whose
    *list* price isn't a deal but a plausible accepted offer would be — shown as a separate
    "**deal if you offer ≈ £X**" alert that never claims the list price is the deal.
  - **Vision flags** (optional, `MARKET_VISION=true` + the `vision` extra). Beyond verifying
    the photo matches the product, flags a **stock photo on a used listing** (dropship/scam
    tell), **possible damage**, and a **multiple-items** lot hint. The CLIP model is
    **preloaded** in the background at startup and every image is **downloaded + embedded once**
    (cached by URL), so checks are cheap and never block.
  - **Stock-image match** (`MARKET_VISION_REFERENCE=true`). Compares each listing's photo
    **image-to-image** against a reference "stock" image of the actual item — the most direct
    "is this really the product?" test — and **drops listings that clearly show something else**.
    The reference is auto-derived from the market (the medoid, the most representative photo
    across comparable listings); pin the exact one per watch with `/refimage <id> <image-url>`
    (clear with `/refimage <id> clear`). Matching deals carry a **📷 Matches the item (NN%)** read.
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
- Optional **per-seller price floor** to mute a seller who spams cheap junk:
  - Add it in brackets when you watch or observe a seller — `/add lithosale [100]` or
    `/observe lithosale 90s [100]` — and only items priced **at or above** that figure
    alert (new, price-drop, ended/restock for the watch list; new listings for observe).
  - No floor means **any price** alerts, so existing sellers are unaffected.
  - The floor is currency-agnostic (it compares the listing's numeric amount) and fails
    open: if a price can't be parsed, the item still alerts.
- Lets you manage sellers from Telegram:
  - `/add sellername [minprice]` — e.g. `/add acme` or `/add lithosale [100]`
  - `/remove sellername`
  - `/list` — sellers with their price floor, if any
  - `/floor sellername [100]` — set/retune a floor later (use `none` to clear); applies to
    the seller's watch and/or observe entry without losing its seen baseline
  - `/status`
  - `/health` — liveness: uptime, time since last poll, seller/watch counts, errors
  - `/check`
  - `/observe sellername [interval] [minprice]` — start fast new-listing alerts
    (e.g. `/observe acme 90s` or `/observe acme 90s [100]`)
  - `/unobserve sellername`
  - `/observing` — show the observe list, intervals, floors, and last check
  - `/interval sellername <time>` — change a seller's observe interval (e.g. `5m`, `1h`)
  - `/watch <terms> [condition:new|used] [under:PRICE] [discount:%] [every:TIME] [exclude:word] [category:ID] [auctions:on] [markets:GB,DE,US]` —
    snipe below-market deals (e.g. `/watch dyson airblade hu02 condition:new under:400 discount:20`)
  - `/unwatch <id>` — stop a market watch (ids shown by `/watches`)
  - `/watches` — list market watches with market estimate, variant, price trend, and last check
  - `/demand <id>` — the demand/liquidity read for a watch
  - `/refimage <id> <image-url>` — pin the reference "stock" photo a watch matches listings
    against (`/refimage <id>` shows it, `/refimage <id> clear` reverts to auto)
  - `/help`
  - **Tap to remove:** `/list`, `/observing` and `/watches` each render a 🗑 button per row —
    tap it to drop that seller/watch without retyping its name or id; the list refreshes in place.
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
ebayspy sellers add sellername --min 100   # only alert on items at/above £100
ebayspy sellers floor sellername 100       # set/retune a floor later (use 'none' to clear)
ebayspy sellers list
ebayspy sellers remove sellername
```

Manage the fast-poll observe list locally:

```powershell
ebayspy observe add sellername 3m
ebayspy observe add sellername 3m --min 100
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

## macOS (local) notes

To run ebayspy on a Mac that sleeps, install two LaunchAgents:

```bash
scripts/install-launchd.sh      # com.ebayspy.tracker  — always-on while awake
scripts/install-wakepoll.sh     # com.ebayspy.wakepoll — wakes a sleeping Mac to poll
sudo scripts/enable-wake-sudo.sh  # one-off: lets the agent arm wakes (pmset) without a password
```

- **Tracker** (`com.ebayspy.tracker`, `ebayspy run`) handles Telegram commands and the
  observe/market fast lanes in real time — but only while the Mac is awake.
- **Wake-poll** (`com.ebayspy.wakepoll`, `ebayspy wakepoll`) runs on a 6-hour calendar
  schedule (00/06/12/18). Each run polls once, sends a heartbeat (if enabled), then arms
  the next wake(s) via `pmset`. The wakes are aligned to the same grid (`EBAYSPY_WAKE_HOURS`,
  `EBAYSPY_WAKE_ARM_COUNT`) so a sleeping Mac comes back up for each slot; arming the next
  two slots lets the schedule self-heal if one run is missed.
- **Heartbeat** (`HEARTBEAT_ENABLED=true`): a periodic "still alive" ping (uptime + health)
  sent when no real alert went out within `HEARTBEAT_INTERVAL_SECONDS`. It fires from both the
  tracker and the wake-poll cycle, so you get a confirmation roughly every wake even with no
  new deals. Set the interval just under `EBAYSPY_WAKE_HOURS` (e.g. `18000` for a 6h wake).

> Scheduled wake-from-sleep is most reliable with the Mac **on power**; on battery with the
> lid closed, macOS may not honour every wake. For 24/7 reliability, run on a VPS instead.

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
