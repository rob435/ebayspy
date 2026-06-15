from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    # Fall back to the default (not False) so a typo like MARKET_HYDRATE=ture
    # doesn't silently disable a default-on feature — just warn loudly.
    log.warning("%s=%r is not a recognized boolean; using default %s (use true/false).", name, value, default)
    return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {value!r}") from None


def _source_env(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower() or default
    if value not in allowed:
        log.warning(
            "%s=%r is not one of %s; using %r.", name, value, sorted(allowed), default
        )
        return default
    return value


def _market_watches_env(name: str) -> list[str]:
    # Queries can contain commas, so market watches are separated by ';'.
    value = os.getenv(name, "")
    return [part.strip() for part in value.split(";") if part.strip()]


def _aliases_env(name: str) -> list[tuple[str, str]]:
    # Format: "ps5=playstation 5;series x=xbox series x"
    pairs = []
    for part in os.getenv(name, "").split(";"):
        variant, sep, canonical = part.partition("=")
        if sep and variant.strip() and canonical.strip():
            pairs.append((variant.strip(), canonical.strip()))
    return pairs


def _fx_rates_env(name: str) -> list[tuple[str, float]]:
    # Format: "GBP=0.79,EUR=0.92" (units per 1 USD)
    pairs = []
    for part in _csv_env(name):
        code, sep, value = part.partition("=")
        if sep:
            try:
                pairs.append((code.strip().upper(), float(value)))
            except ValueError:
                continue
    return pairs


def _csv_env(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [part.strip() for part in value.split(",") if part.strip()]


def _username_env(name: str) -> list[str]:
    return [part.removeprefix("@").lower() for part in _csv_env(name)]


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str | None
    telegram_allowed_chat_ids: tuple[str, ...]
    telegram_allowed_usernames: tuple[str, ...]
    telegram_send_photos: bool
    ebay_app_id: str | None
    ebay_client_secret: str | None
    ebay_global_id: str
    sqlite_path: Path
    poll_interval_seconds: int
    seller_check_delay_seconds: int
    max_items_per_seller: int
    detail_concurrency: int
    notify_existing_on_first_run: bool
    http_timeout_seconds: int
    seed_sellers: tuple[str, ...]
    observe_interval_seconds: int
    observe_min_interval_seconds: int
    observe_sellers: tuple[str, ...]
    market_interval_seconds: int
    market_min_interval_seconds: int
    market_discount_percent: int
    market_sample_size: int
    market_min_deal_ratio: float
    market_max_deals_per_cycle: int
    market_min_sample: int
    market_match_coverage: float
    market_fuzzy_threshold: float
    market_semantic: bool
    market_semantic_threshold: float
    market_hydrate: bool
    market_hydrate_limit: int
    market_price_source: str
    market_vision: bool
    market_vision_match_threshold: float
    market_min_dispersion: float
    market_deal_scan: bool
    market_resale_fee_percent: float
    market_resale_fee_fixed: float
    market_auctions_default: bool
    market_snipe_window_seconds: int
    market_turbo_interval_seconds: int
    market_low_bid_count: int
    market_nobid_discount_percent: int
    market_demand_grace_seconds: int
    market_demand_window_days: int
    market_demand_min_events: int
    market_arbitrage_threshold: float
    market_arbitrage_interval_seconds: int
    market_health_threshold: int
    market_risk_warn: int
    market_risk_max: int
    # Price-drop alerts on watched sellers' still-active listings.
    seller_price_drop_percent: int
    # Best-offer-aware deal detection.
    market_offer_aware: bool
    market_expected_offer_discount: float
    # vision++ extra classifiers (CLIP, optional).
    market_vision_stock_threshold: float
    market_vision_damage_threshold: float
    market_vision_count_hint: bool
    market_vision_count_threshold: float
    # Feedback loop (👍/👎 on deals tunes the per-watch discount threshold).
    market_feedback_enabled: bool
    market_feedback_step: float
    market_feedback_relax_step: float
    market_feedback_max_nudge: float
    # Health heartbeat.
    heartbeat_enabled: bool
    heartbeat_interval_seconds: int
    # State backup (online SQLite snapshots).
    backup_dir: Path
    backup_keep: int
    backup_interval_seconds: int
    fx_rates: tuple[tuple[str, float], ...]
    market_aliases: tuple[tuple[str, str], ...]
    market_watches: tuple[str, ...]
    wake_ahead_hours: float
    wake_netwait_seconds: float

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        app_id = os.getenv("EBAY_APP_ID", "").strip() or None
        client_secret = os.getenv("EBAY_CLIENT_SECRET", "").strip() or None
        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            telegram_allowed_chat_ids=tuple(_csv_env("TELEGRAM_ALLOWED_CHAT_IDS")),
            telegram_allowed_usernames=tuple(_username_env("TELEGRAM_ALLOWED_USERNAMES")),
            telegram_send_photos=_bool_env("TELEGRAM_SEND_PHOTOS", True),
            ebay_app_id=app_id,
            ebay_client_secret=client_secret,
            ebay_global_id=os.getenv("EBAY_GLOBAL_ID", "EBAY-US").strip() or "EBAY-US",
            sqlite_path=Path(os.getenv("SQLITE_PATH", "ebayspy.sqlite3")),
            poll_interval_seconds=_int_env("POLL_INTERVAL_SECONDS", 900),
            seller_check_delay_seconds=_int_env("SELLER_CHECK_DELAY_SECONDS", 0),
            max_items_per_seller=_int_env("MAX_ITEMS_PER_SELLER", 20),
            detail_concurrency=_int_env("DETAIL_CONCURRENCY", 5),
            notify_existing_on_first_run=_bool_env("NOTIFY_EXISTING_ON_FIRST_RUN", False),
            http_timeout_seconds=_int_env("HTTP_TIMEOUT_SECONDS", 20),
            seed_sellers=tuple(_csv_env("SELLERS")),
            observe_interval_seconds=_int_env("OBSERVE_INTERVAL_SECONDS", 180),
            observe_min_interval_seconds=_int_env("OBSERVE_MIN_INTERVAL_SECONDS", 30),
            observe_sellers=tuple(_csv_env("OBSERVE_SELLERS")),
            market_interval_seconds=_int_env("MARKET_INTERVAL_SECONDS", 600),
            market_min_interval_seconds=_int_env("MARKET_MIN_INTERVAL_SECONDS", 120),
            market_discount_percent=_int_env("MARKET_DISCOUNT_PERCENT", 15),
            market_sample_size=_int_env("MARKET_SAMPLE_SIZE", 200),
            market_min_deal_ratio=_float_env("MARKET_MIN_DEAL_RATIO", 0.4),
            market_max_deals_per_cycle=_int_env("MARKET_MAX_DEALS_PER_CYCLE", 5),
            market_min_sample=_int_env("MARKET_MIN_SAMPLE", 5),
            market_match_coverage=_float_env("MARKET_MATCH_COVERAGE", 0.6),
            market_fuzzy_threshold=_float_env("MARKET_FUZZY_THRESHOLD", 0.88),
            market_semantic=_bool_env("MARKET_SEMANTIC", True),
            market_semantic_threshold=_float_env("MARKET_SEMANTIC_THRESHOLD", 0.6),
            market_hydrate=_bool_env("MARKET_HYDRATE", True),
            market_hydrate_limit=_int_env("MARKET_HYDRATE_LIMIT", 20),
            market_price_source=_source_env(
                "MARKET_PRICE_SOURCE", "listings", {"listings", "insights"}
            ),
            market_vision=_bool_env("MARKET_VISION", False),
            market_vision_match_threshold=_float_env("MARKET_VISION_MATCH_THRESHOLD", 0.18),
            market_min_dispersion=_float_env("MARKET_MIN_DISPERSION", 0.12),
            market_deal_scan=_bool_env("MARKET_DEAL_SCAN", False),
            market_resale_fee_percent=_float_env("MARKET_RESALE_FEE_PERCENT", 12.8),
            market_resale_fee_fixed=_float_env("MARKET_RESALE_FEE_FIXED", 0.30),
            market_auctions_default=_bool_env("MARKET_AUCTIONS_DEFAULT", False),
            market_snipe_window_seconds=_int_env("MARKET_SNIPE_WINDOW_SECONDS", 600),
            market_turbo_interval_seconds=_int_env("MARKET_TURBO_INTERVAL_SECONDS", 45),
            market_low_bid_count=_int_env("MARKET_LOW_BID_COUNT", 1),
            market_nobid_discount_percent=_int_env("MARKET_NOBID_DISCOUNT_PERCENT", 5),
            market_demand_grace_seconds=_int_env("MARKET_DEMAND_GRACE_SECONDS", 86400),
            market_demand_window_days=_int_env("MARKET_DEMAND_WINDOW_DAYS", 14),
            market_demand_min_events=_int_env("MARKET_DEMAND_MIN_EVENTS", 3),
            market_arbitrage_threshold=_float_env("MARKET_ARBITRAGE_THRESHOLD", 20.0),
            market_arbitrage_interval_seconds=_int_env("MARKET_ARBITRAGE_INTERVAL_SECONDS", 3600),
            market_health_threshold=_int_env("MARKET_HEALTH_THRESHOLD", 5),
            market_risk_warn=_int_env("MARKET_RISK_WARN", 40),
            market_risk_max=_int_env("MARKET_RISK_MAX", 100),
            seller_price_drop_percent=_int_env("SELLER_PRICE_DROP_PERCENT", 5),
            market_offer_aware=_bool_env("MARKET_OFFER_AWARE", False),
            market_expected_offer_discount=_float_env("MARKET_EXPECTED_OFFER_DISCOUNT", 10.0),
            market_vision_stock_threshold=_float_env("MARKET_VISION_STOCK_THRESHOLD", 0.55),
            market_vision_damage_threshold=_float_env("MARKET_VISION_DAMAGE_THRESHOLD", 0.55),
            market_vision_count_hint=_bool_env("MARKET_VISION_COUNT_HINT", False),
            market_vision_count_threshold=_float_env("MARKET_VISION_COUNT_THRESHOLD", 0.55),
            market_feedback_enabled=_bool_env("MARKET_FEEDBACK_ENABLED", True),
            market_feedback_step=_float_env("MARKET_FEEDBACK_STEP", 2.0),
            market_feedback_relax_step=_float_env("MARKET_FEEDBACK_RELAX_STEP", 1.0),
            market_feedback_max_nudge=_float_env("MARKET_FEEDBACK_MAX_NUDGE", 10.0),
            heartbeat_enabled=_bool_env("HEARTBEAT_ENABLED", False),
            heartbeat_interval_seconds=_int_env("HEARTBEAT_INTERVAL_SECONDS", 86400),
            backup_dir=Path(os.getenv("BACKUP_DIR", "backups")),
            backup_keep=_int_env("BACKUP_KEEP", 7),
            backup_interval_seconds=_int_env("BACKUP_INTERVAL_SECONDS", 86400),
            fx_rates=tuple(_fx_rates_env("FX_RATES")),
            market_aliases=tuple(_aliases_env("ALIASES")),
            market_watches=tuple(_market_watches_env("MARKET_WATCHES")),
            wake_ahead_hours=_float_env("EBAYSPY_WAKE_HOURS", 6.0),
            wake_netwait_seconds=_float_env("EBAYSPY_WAKE_NETWAIT", 20.0),
        )

    def require_telegram(self) -> None:
        if not self.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required. Add it to .env or the environment.")

    def require_ebay(self) -> None:
        if not self.ebay_app_id or not self.ebay_client_secret:
            raise SystemExit(
                "EBAY_APP_ID and EBAY_CLIENT_SECRET are required. Create an application "
                "keyset at https://developer.ebay.com and add them to .env."
            )
