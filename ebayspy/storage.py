from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .models import Listing

EBAY_USERNAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"
TELEGRAM_USERNAME_RE = r"^[A-Za-z][A-Za-z0-9_]{4,31}$"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            create table if not exists sellers (
                username text primary key,
                created_at text not null default current_timestamp
            );

            create table if not exists observed_sellers (
                username text primary key,
                interval_seconds integer,
                created_at text not null default current_timestamp,
                last_observed_at text,
                last_ok_at text,
                last_error text,
                last_new_count integer not null default 0
            );

            create table if not exists seen_items (
                item_id text primary key,
                seller text not null,
                title text not null,
                url text not null,
                first_seen_at text not null default current_timestamp,
                notified_at text
            );

            create table if not exists active_items (
                item_id text primary key,
                seller text not null,
                title text not null,
                price text not null,
                url text not null,
                description text not null default '',
                listed_at text,
                image_url text,
                listing_type text not null default '',
                category text not null default '',
                quantity_available text not null default '',
                first_seen_at text not null default current_timestamp,
                last_seen_at text not null default current_timestamp,
                ended_at text,
                ended_notified_at text
            );

            create table if not exists telegram_chats (
                chat_id text primary key,
                username text,
                created_at text not null default current_timestamp
            );

            create table if not exists seller_checks (
                username text primary key,
                last_checked_at text not null default current_timestamp,
                last_ok_at text,
                last_error text,
                last_listing_count integer not null default 0,
                last_new_count integer not null default 0,
                last_ended_count integer not null default 0
            );

            create table if not exists metadata (
                key text primary key,
                value text not null
            );
            """
        )
        self.conn.commit()
        self._ensure_listing_columns()

    def _ensure_listing_columns(self) -> None:
        seen_columns = self._table_columns("seen_items")
        for column in ("listing_type", "category", "quantity_available"):
            if column not in seen_columns:
                self.conn.execute(
                    f"alter table seen_items add column {column} text not null default ''"
                )
        active_columns = self._table_columns("active_items")
        active_defaults = {
            "description": "text not null default ''",
            "listed_at": "text",
            "image_url": "text",
            "listing_type": "text not null default ''",
            "category": "text not null default ''",
            "quantity_available": "text not null default ''",
            "ended_at": "text",
            "ended_notified_at": "text",
        }
        for column, definition in active_defaults.items():
            if column not in active_columns:
                self.conn.execute(f"alter table active_items add column {column} {definition}")
        check_columns = self._table_columns("seller_checks")
        if "last_ended_count" not in check_columns:
            self.conn.execute(
                "alter table seller_checks add column last_ended_count integer not null default 0"
            )
        chat_columns = self._table_columns("telegram_chats")
        if "username" not in chat_columns:
            self.conn.execute("alter table telegram_chats add column username text")
        self.conn.commit()

    def _table_columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"pragma table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def record_check(
        self,
        username: str,
        listing_count: int,
        new_count: int,
        ended_count: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            insert into seller_checks(
                username,
                last_checked_at,
                last_ok_at,
                last_error,
                last_listing_count,
                last_new_count,
                last_ended_count
            )
            values (
                ?,
                current_timestamp,
                case when ? is null then current_timestamp else null end,
                ?,
                ?,
                ?,
                ?
            )
            on conflict(username) do update set
                last_checked_at = current_timestamp,
                last_ok_at = case
                    when excluded.last_error is null then current_timestamp
                    else seller_checks.last_ok_at
                end,
                last_error = excluded.last_error,
                last_listing_count = excluded.last_listing_count,
                last_new_count = excluded.last_new_count,
                last_ended_count = excluded.last_ended_count
            """,
            (username, error, error, listing_count, new_count, ended_count),
        )
        self.conn.commit()

    def status_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            select
                s.username,
                c.last_checked_at,
                c.last_ok_at,
                c.last_error,
                c.last_listing_count,
                c.last_new_count,
                c.last_ended_count
            from sellers s
            left join seller_checks c on lower(c.username) = lower(s.username)
            order by lower(s.username)
            """
        ).fetchall()

    def close(self) -> None:
        self.conn.close()

    def add_seller(self, username: str) -> bool:
        username = normalize_ebay_username(username)
        if not username:
            return False
        if self.has_seller(username):
            return False
        self.conn.execute("insert or ignore into sellers(username) values (?)", (username,))
        self.conn.commit()
        return True

    def has_seller(self, username: str) -> bool:
        row = self.conn.execute(
            "select 1 from sellers where lower(username) = lower(?) limit 1", (username,)
        ).fetchone()
        return row is not None

    def remove_seller(self, username: str) -> bool:
        cur = self.conn.execute("delete from sellers where lower(username) = lower(?)", (username,))
        self.conn.commit()
        return cur.rowcount > 0

    def rename_seller(self, old_username: str, new_username: str) -> bool:
        old_username = old_username.strip()
        new_username = new_username.strip()
        if not old_username or not new_username or old_username.lower() == new_username.lower():
            return False
        self.conn.execute("insert or ignore into sellers(username) values (?)", (new_username,))
        cur = self.conn.execute(
            "delete from sellers where lower(username) = lower(?)", (old_username,)
        )
        self.conn.execute(
            "update seen_items set seller = ? where lower(seller) = lower(?)",
            (new_username, old_username),
        )
        self.conn.execute(
            "update active_items set seller = ? where lower(seller) = lower(?)",
            (new_username, old_username),
        )
        self.conn.execute("delete from seller_checks where lower(username) = lower(?)", (old_username,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_sellers(self) -> list[str]:
        rows = self.conn.execute("select username from sellers order by lower(username)").fetchall()
        return [row["username"] for row in rows]

    def add_observed_seller(self, username: str, interval_seconds: int | None = None) -> bool:
        username = normalize_ebay_username(username)
        if not username:
            return False
        if self.has_observed_seller(username):
            return False
        self.conn.execute(
            "insert or ignore into observed_sellers(username, interval_seconds) values (?, ?)",
            (username, interval_seconds),
        )
        self.conn.commit()
        return True

    def has_observed_seller(self, username: str) -> bool:
        row = self.conn.execute(
            "select 1 from observed_sellers where lower(username) = lower(?) limit 1", (username,)
        ).fetchone()
        return row is not None

    def remove_observed_seller(self, username: str) -> bool:
        cur = self.conn.execute(
            "delete from observed_sellers where lower(username) = lower(?)", (username,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_observed_interval(self, username: str, interval_seconds: int | None) -> bool:
        cur = self.conn.execute(
            "update observed_sellers set interval_seconds = ? where lower(username) = lower(?)",
            (interval_seconds, username),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_observed_sellers(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from observed_sellers order by lower(username)"
        ).fetchall()

    def observed_seller_has_successful_check(self, username: str) -> bool:
        row = self.conn.execute(
            """
            select 1 from observed_sellers
            where lower(username) = lower(?)
              and last_ok_at is not null
            limit 1
            """,
            (username,),
        ).fetchone()
        return row is not None

    def record_observe_check(
        self, username: str, new_count: int, error: str | None = None
    ) -> None:
        self.conn.execute(
            """
            update observed_sellers set
                last_observed_at = current_timestamp,
                last_ok_at = case when ? is null then current_timestamp else last_ok_at end,
                last_error = ?,
                last_new_count = ?
            where lower(username) = lower(?)
            """,
            (error, error, new_count, username),
        )
        self.conn.commit()

    def recent_active_item_ids(self, seller: str, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            """
            select item_id from active_items
            where lower(seller) = lower(?)
            order by last_seen_at desc
            limit ?
            """,
            (seller, limit),
        ).fetchall()
        return [row["item_id"] for row in rows]

    def add_chat(self, chat_id: str, username: str | None = None) -> None:
        normalized_username = normalize_telegram_username(username)
        self.conn.execute(
            """
            insert into telegram_chats(chat_id, username) values (?, ?)
            on conflict(chat_id) do update set
                username = coalesce(excluded.username, telegram_chats.username)
            """,
            (str(chat_id), normalized_username),
        )
        self.conn.commit()

    def list_chats(self) -> list[str]:
        rows = self.conn.execute("select chat_id from telegram_chats order by created_at").fetchall()
        return [row["chat_id"] for row in rows]

    def list_chat_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select chat_id, username from telegram_chats order by created_at"
        ).fetchall()

    def add_allowed_username(self, username: str) -> bool:
        username = normalize_telegram_username(username)
        if not is_valid_telegram_username(username):
            return False
        usernames = set(self.list_allowed_usernames())
        if username in usernames:
            return False
        usernames.add(username)
        self.set_meta("allowed_usernames", ",".join(sorted(usernames)))
        return True

    def remove_allowed_username(self, username: str) -> bool:
        username = normalize_telegram_username(username)
        usernames = set(self.list_allowed_usernames())
        if username not in usernames:
            return False
        usernames.remove(username)
        self.set_meta("allowed_usernames", ",".join(sorted(usernames)))
        return True

    def list_allowed_usernames(self) -> list[str]:
        value = self.get_meta("allowed_usernames") or ""
        return sorted(
            {
                normalized
                for part in value.split(",")
                if (normalized := normalize_telegram_username(part))
            }
        )

    def seller_has_seen_items(self, seller: str) -> bool:
        row = self.conn.execute(
            "select 1 from seen_items where lower(seller) = lower(?) limit 1", (seller,)
        ).fetchone()
        return row is not None

    def seller_has_successful_check(self, seller: str) -> bool:
        row = self.conn.execute(
            """
            select 1 from seller_checks
            where lower(username) = lower(?)
              and last_ok_at is not null
            limit 1
            """,
            (seller,),
        ).fetchone()
        return row is not None

    def is_seen(self, item_id: str) -> bool:
        row = self.conn.execute("select 1 from seen_items where item_id = ?", (item_id,)).fetchone()
        return row is not None

    def mark_seen(self, listing: Listing, notified: bool) -> None:
        self.conn.execute(
            """
            insert or ignore into seen_items(
                item_id,
                seller,
                title,
                url,
                notified_at,
                listing_type,
                category,
                quantity_available
            )
            values (?, ?, ?, ?, case when ? then current_timestamp else null end, ?, ?, ?)
            """,
            (
                listing.item_id,
                listing.seller,
                listing.title,
                listing.url,
                1 if notified else 0,
                listing.listing_type,
                listing.category,
                listing.quantity_available,
            ),
        )
        self.conn.commit()

    def upsert_active_listings(self, listings: list[Listing]) -> None:
        self.conn.executemany(
            """
            insert into active_items(
                item_id,
                seller,
                title,
                price,
                url,
                description,
                listed_at,
                image_url,
                listing_type,
                category,
                quantity_available,
                last_seen_at,
                ended_at,
                ended_notified_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp, null, null)
            on conflict(item_id) do update set
                seller = excluded.seller,
                title = excluded.title,
                price = excluded.price,
                url = excluded.url,
                description = excluded.description,
                listed_at = excluded.listed_at,
                image_url = excluded.image_url,
                listing_type = excluded.listing_type,
                category = excluded.category,
                quantity_available = excluded.quantity_available,
                last_seen_at = current_timestamp,
                ended_at = null,
                ended_notified_at = null
            """,
            [
                (
                    item.item_id,
                    item.seller,
                    item.title,
                    item.price,
                    item.url,
                    item.description,
                    item.listed_at,
                    item.image_url,
                    item.listing_type,
                    item.category,
                    item.quantity_available,
                )
                for item in listings
            ],
        )
        self.conn.commit()

    def ended_candidates(self, seller: str, active_item_ids: set[str]) -> list[Listing]:
        if active_item_ids:
            placeholders = ",".join("?" for _ in active_item_ids)
            query = f"""
                select * from active_items
                where lower(seller) = lower(?)
                  and ended_notified_at is null
                  and ended_at is null
                  and item_id not in ({placeholders})
                order by last_seen_at desc
            """
            params: tuple[str, ...] = (seller, *active_item_ids)
        else:
            query = """
                select * from active_items
                where lower(seller) = lower(?)
                  and ended_notified_at is null
                  and ended_at is null
                order by last_seen_at desc
            """
            params = (seller,)
        rows = self.conn.execute(query, params).fetchall()
        return [self._listing_from_row(row) for row in rows]

    def quantity_increase_candidates(self, listings: list[Listing]) -> list[tuple[Listing, int, int]]:
        increases = []
        for listing in listings:
            current_quantity = _parse_quantity(listing.quantity_available)
            if current_quantity is None:
                continue
            row = self.conn.execute(
                """
                select quantity_available from active_items
                where item_id = ?
                """,
                (listing.item_id,),
            ).fetchone()
            if row is None:
                continue
            previous_quantity = _parse_quantity(row["quantity_available"])
            if previous_quantity is not None and current_quantity > previous_quantity:
                increases.append((listing, previous_quantity, current_quantity))
        return increases

    def mark_ended_notified(self, item_id: str) -> None:
        self.conn.execute(
            """
            update active_items
            set ended_at = coalesce(ended_at, current_timestamp),
                ended_notified_at = current_timestamp
            where item_id = ?
            """,
            (item_id,),
        )
        self.conn.commit()

    def _listing_from_row(self, row: sqlite3.Row) -> Listing:
        return Listing(
            item_id=row["item_id"],
            seller=row["seller"],
            title=row["title"],
            price=row["price"],
            url=row["url"],
            description=row["description"],
            listed_at=row["listed_at"],
            image_url=row["image_url"],
            listing_type=row["listing_type"],
            category=row["category"],
            quantity_available=row["quantity_available"],
        )


    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("select value from metadata where key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert into metadata(key, value) values (?, ?) "
            "on conflict(key) do update set value = excluded.value",
            (key, value),
        )
        self.conn.commit()


def format_status_rows(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No sellers configured."
    lines = []
    for row in rows:
        checked = row["last_checked_at"] or "never"
        error = row["last_error"]
        if error:
            lines.append(f"{row['username']}: ERROR at {checked}: {error[:120]}")
        else:
            count = row["last_listing_count"] if row["last_listing_count"] is not None else "-"
            new = row["last_new_count"] if row["last_new_count"] is not None else "-"
            ended = row["last_ended_count"] if row["last_ended_count"] is not None else "-"
            lines.append(
                f"{row['username']}: ok, {count} active, {new} new, {ended} ended, checked {checked}"
            )
    return "\n".join(lines)


def format_observed_rows(rows: list[sqlite3.Row], default_interval_seconds: int) -> str:
    if not rows:
        return "No observed sellers yet. Add one with /observe sellername [interval]."
    lines = []
    for row in rows:
        interval = row["interval_seconds"] or default_interval_seconds
        every = format_interval(interval)
        error = row["last_error"]
        observed = row["last_observed_at"] or "never"
        if error:
            lines.append(f"{row['username']}: every {every}, ERROR at {observed}: {error[:120]}")
        else:
            new = row["last_new_count"] if row["last_new_count"] is not None else "-"
            lines.append(
                f"{row['username']}: every {every}, {new} new last check, checked {observed}"
            )
    return "Observing:\n" + "\n".join(lines)


def parse_interval(value: str | None) -> int | None:
    """Parse a human interval like '90', '90s', '3m', or '1h' into seconds."""
    import re

    text = (value or "").strip().lower()
    match = re.fullmatch(r"(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)?", text)
    if not match:
        return None
    quantity = int(match.group(1))
    unit = match.group(2) or "s"
    if unit.startswith("h"):
        return quantity * 3600
    if unit.startswith("m"):
        return quantity * 60
    return quantity


def format_interval(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def normalize_telegram_username(username: str | None) -> str:
    return (username or "").strip().removeprefix("@").lower()


def normalize_ebay_username(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value or value.lower().startswith("www."):
        parsed = urlparse(value if "://" in value else f"https://{value}")
        query_seller = parse_qs(parsed.query).get("_ssn", [""])[0]
        if query_seller:
            return unquote(query_seller).strip()
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        for marker in ("usr", "str"):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                return parts[parts.index(marker) + 1].strip()
    return value.removeprefix("@").strip()


def is_valid_ebay_username(username: str) -> bool:
    import re

    return re.fullmatch(EBAY_USERNAME_RE, username) is not None


def is_valid_telegram_username(username: str) -> bool:
    import re

    return re.fullmatch(TELEGRAM_USERNAME_RE, username) is not None


def _parse_quantity(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value.isdigit():
        return None
    return int(value)
