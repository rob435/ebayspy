from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
from datetime import datetime

from . import wake
from .config import Config
from .service import EbaySpyService
from .storage import (
    Store,
    format_interval,
    format_market_rows,
    format_observed_rows,
    format_price_floor,
    format_seller_rows,
    format_status_rows,
    parse_interval,
    parse_price_floor,
)

log = logging.getLogger("ebayspy.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebayspy")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="Run the Telegram tracker (watch + observe loops)")
    subparsers.add_parser("check", help="Run one watch-list poll immediately")
    subparsers.add_parser(
        "wakepoll", help="One poll, then arm the next wake (run by the wakepoll LaunchAgent)"
    )
    subparsers.add_parser("status", help="Show the last check status for each seller")

    backup = subparsers.add_parser("backup", help="Write a timestamped DB snapshot")
    backup.add_argument("--dir", help="Destination directory (default: BACKUP_DIR)")
    backup.add_argument("--keep", type=int, help="Snapshots to retain (default: BACKUP_KEEP)")

    sellers = subparsers.add_parser("sellers", help="Manage watched sellers")
    seller_sub = sellers.add_subparsers(dest="seller_command", required=True)
    add = seller_sub.add_parser("add", help="Add a seller")
    add.add_argument("username")
    add.add_argument(
        "--min", type=float, dest="min_price", help="Only alert on items at/above this price"
    )
    remove = seller_sub.add_parser("remove", help="Remove a seller")
    remove.add_argument("username")
    floor = seller_sub.add_parser("floor", help="Set/clear a seller's price floor")
    floor.add_argument("username")
    floor.add_argument(
        "price", nargs="?", help="Minimum price, or 'none' to clear (default: clear)"
    )
    seller_sub.add_parser("list", help="List sellers")

    observe = subparsers.add_parser("observe", help="Manage fast-poll observe-list sellers")
    observe_sub = observe.add_subparsers(dest="observe_command", required=True)
    observe_add = observe_sub.add_parser("add", help="Add a seller to the observe list")
    observe_add.add_argument("username")
    observe_add.add_argument("interval", nargs="?", help="Optional interval, e.g. 90s, 3m, 1h")
    observe_add.add_argument(
        "--min", type=float, dest="min_price", help="Only alert on items at/above this price"
    )
    observe_remove = observe_sub.add_parser("remove", help="Remove a seller from the observe list")
    observe_remove.add_argument("username")
    observe_sub.add_parser("list", help="List observe-list sellers")

    market = subparsers.add_parser("market", help="Manage below-market deal watches")
    market_sub = market.add_subparsers(dest="market_command", required=True)
    market_add = market_sub.add_parser("add", help="Add a market watch")
    market_add.add_argument("query", help="Search terms, e.g. 'dyson airblade hu02'")
    market_add.add_argument("--condition", choices=["new", "used"])
    market_add.add_argument("--under", type=float, help="Max price to consider")
    market_add.add_argument("--discount", type=int, help="Discount %% under market that's a deal")
    market_add.add_argument("--category", help="Numeric eBay category id to focus on")
    market_remove = market_sub.add_parser("remove", help="Remove a market watch by id")
    market_remove.add_argument("id", type=int)
    market_sub.add_parser("list", help="List market watches")
    return parser


async def _run_once(config: Config) -> None:
    service = EbaySpyService(config)
    try:
        count = await service.check_once()
        print(f"Check complete. Alerts sent: {count}")
    finally:
        await service.close()


async def _wakepoll(config: Config) -> None:
    """One poll while held awake, then arm the next wakes so a sleeping Mac
    returns for the following slot. Run directly by the wakepoll LaunchAgent."""
    netwait = config.wake_netwait_seconds
    hours = config.wake_ahead_hours

    caffeinate: subprocess.Popen | None = None
    try:  # keep the Mac awake for the duration; released when this process exits
        caffeinate = subprocess.Popen(wake.caffeinate_argv(os.getpid()))
    except OSError:
        log.warning("could not start caffeinate; continuing without it", exc_info=True)

    try:
        if netwait > 0:
            await asyncio.sleep(netwait)  # let Wi-Fi reconnect after waking
        service = EbaySpyService(config)
        try:
            count = await service.check_once()
            print(f"Check complete. Alerts sent: {count}")
            # Always emit a heartbeat (when enabled) so a woken-from-sleep cycle
            # still produces a guaranteed ~6-hourly status message even when no
            # new listings or deals turned up. No-ops when nothing is due.
            await service._maybe_send_heartbeat()
        finally:
            await service.close()
    finally:
        _arm_next_wakes(wake.next_wake_times(datetime.now(), hours, config.wake_arm_count))
        if caffeinate is not None:
            caffeinate.terminate()


def _arm_next_wakes(whens: list[str]) -> None:
    """Arm one pmset wake per grid slot in ``whens``; log and carry on if the
    passwordless sudoers rule (scripts/enable-wake-sudo.sh) isn't installed."""
    for when in whens:
        try:
            result = subprocess.run(
                wake.arm_wake_argv(when), capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                log.info("armed next wake: %s", when)
            else:
                log.warning(
                    "could not arm wake %s (%s); run once: sudo ./scripts/enable-wake-sudo.sh",
                    when,
                    (result.stderr or "").strip() or f"exit {result.returncode}",
                )
        except (OSError, subprocess.SubprocessError):
            log.warning("could not arm wake %s", when, exc_info=True)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.load()

    if args.command == "run":
        asyncio.run(EbaySpyService(config).run_forever())
    elif args.command == "check":
        asyncio.run(_run_once(config))
    elif args.command == "wakepoll":
        asyncio.run(_wakepoll(config))
    elif args.command == "status":
        store = Store(config.sqlite_path)
        try:
            print(format_status_rows(store.status_rows()))
        finally:
            store.close()
    elif args.command == "backup":
        store = Store(config.sqlite_path)
        try:
            dest = args.dir or config.backup_dir
            keep = args.keep if args.keep is not None else config.backup_keep
            print(f"Backup written: {store.backup(dest, keep=keep)}")
        finally:
            store.close()
    elif args.command == "sellers":
        store = Store(config.sqlite_path)
        try:
            if args.seller_command == "add":
                min_price = args.min_price if args.min_price and args.min_price > 0 else None
                store.add_seller(args.username, min_price)
                floor = format_price_floor(min_price)
                suffix = f" (alerting only on items {floor})" if floor else ""
                print(f"Added seller: {args.username}{suffix}")
            elif args.seller_command == "remove":
                print("Removed." if store.remove_seller(args.username) else "Seller was not found.")
            elif args.seller_command == "floor":
                floor_ok, floor = parse_price_floor(args.price)
                if not floor_ok:
                    print("Price floor must be a number, or 'none' to clear.")
                elif not store.set_seller_min_price(args.username, floor):
                    print(f"{args.username} is not on the watch list.")
                else:
                    tag = format_price_floor(floor)
                    print(
                        f"{args.username}: alerting on items {tag}."
                        if tag
                        else f"{args.username}: alerting on items at any price."
                    )
            elif args.seller_command == "list":
                print(format_seller_rows(store.list_seller_rows()))
        finally:
            store.close()
    elif args.command == "observe":
        store = Store(config.sqlite_path)
        try:
            if args.observe_command == "add":
                interval = parse_interval(args.interval) if args.interval else None
                min_price = args.min_price if args.min_price and args.min_price > 0 else None
                floor = format_price_floor(min_price)
                suffix = f", alerting only on items {floor}" if floor else ""
                if args.interval and interval is None:
                    print("Interval must look like 90s, 3m, or 1h.")
                elif interval is not None:
                    interval = max(config.observe_min_interval_seconds, interval)
                    store.add_observed_seller(args.username, interval, min_price)
                    print(
                        f"Observing seller: {args.username} "
                        f"every {format_interval(interval)}{suffix}"
                    )
                else:
                    store.add_observed_seller(args.username, None, min_price)
                    print(f"Observing seller: {args.username}{suffix}")
            elif args.observe_command == "remove":
                removed = store.remove_observed_seller(args.username)
                print("Removed." if removed else "Seller was not on the observe list.")
            elif args.observe_command == "list":
                rows = store.list_observed_sellers()
                print(format_observed_rows(rows, config.observe_interval_seconds))
        finally:
            store.close()
    elif args.command == "market":
        store = Store(config.sqlite_path)
        try:
            if args.market_command == "add":
                watch_id = store.add_market_watch(
                    args.query,
                    condition=args.condition,
                    discount_percent=args.discount,
                    max_price=args.under,
                    category_id=args.category,
                )
                if watch_id is None:
                    print(f"Already watching the market for: {args.query}")
                else:
                    print(f"Watching the market for “{args.query}” (#{watch_id}).")
            elif args.market_command == "remove":
                print("Removed." if store.remove_market_watch(args.id) else "No watch with that id.")
            elif args.market_command == "list":
                print(
                    format_market_rows(
                        store.list_market_watches(),
                        config.market_interval_seconds,
                        config.market_discount_percent,
                    )
                )
        finally:
            store.close()
