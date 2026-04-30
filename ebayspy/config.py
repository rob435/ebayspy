from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


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
    ebay_app_id: str | None
    ebay_client_secret: str | None
    ebay_global_id: str
    sqlite_path: Path
    poll_interval_seconds: int
    max_items_per_seller: int
    description_concurrency: int
    notify_existing_on_first_run: bool
    http_timeout_seconds: int
    seed_sellers: tuple[str, ...]

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
            ebay_app_id=app_id,
            ebay_client_secret=client_secret,
            ebay_global_id=os.getenv("EBAY_GLOBAL_ID", "EBAY-US").strip() or "EBAY-US",
            sqlite_path=Path(os.getenv("SQLITE_PATH", "ebayspy.sqlite3")),
            poll_interval_seconds=_int_env("POLL_INTERVAL_SECONDS", 900),
            max_items_per_seller=_int_env("MAX_ITEMS_PER_SELLER", 20),
            description_concurrency=_int_env("DESCRIPTION_CONCURRENCY", 5),
            notify_existing_on_first_run=_bool_env("NOTIFY_EXISTING_ON_FIRST_RUN", False),
            http_timeout_seconds=_int_env("HTTP_TIMEOUT_SECONDS", 20),
            seed_sellers=tuple(_csv_env("SELLERS")),
        )

    def require_telegram(self) -> None:
        if not self.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required. Add it to .env or the environment.")
