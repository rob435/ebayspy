from __future__ import annotations

import argparse
import asyncio
import logging

from .config import Config
from .service import EbaySpyService
from .storage import (
    Store,
    format_interval,
    format_observed_rows,
    format_status_rows,
    parse_interval,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebayspy")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="Run the Telegram tracker (watch + observe loops)")
    subparsers.add_parser("check", help="Run one watch-list poll immediately")
    subparsers.add_parser("status", help="Show the last check status for each seller")

    sellers = subparsers.add_parser("sellers", help="Manage watched sellers")
    seller_sub = sellers.add_subparsers(dest="seller_command", required=True)
    add = seller_sub.add_parser("add", help="Add a seller")
    add.add_argument("username")
    remove = seller_sub.add_parser("remove", help="Remove a seller")
    remove.add_argument("username")
    seller_sub.add_parser("list", help="List sellers")

    observe = subparsers.add_parser("observe", help="Manage fast-poll observe-list sellers")
    observe_sub = observe.add_subparsers(dest="observe_command", required=True)
    observe_add = observe_sub.add_parser("add", help="Add a seller to the observe list")
    observe_add.add_argument("username")
    observe_add.add_argument("interval", nargs="?", help="Optional interval, e.g. 90s, 3m, 1h")
    observe_remove = observe_sub.add_parser("remove", help="Remove a seller from the observe list")
    observe_remove.add_argument("username")
    observe_sub.add_parser("list", help="List observe-list sellers")
    return parser


async def _run_once(config: Config) -> None:
    service = EbaySpyService(config)
    try:
        count = await service.check_once()
        print(f"Check complete. Alerts sent: {count}")
    finally:
        await service.close()


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
    elif args.command == "status":
        store = Store(config.sqlite_path)
        try:
            print(format_status_rows(store.status_rows()))
        finally:
            store.close()
    elif args.command == "sellers":
        store = Store(config.sqlite_path)
        try:
            if args.seller_command == "add":
                store.add_seller(args.username)
                print(f"Added seller: {args.username}")
            elif args.seller_command == "remove":
                print("Removed." if store.remove_seller(args.username) else "Seller was not found.")
            elif args.seller_command == "list":
                sellers = store.list_sellers()
                print("\n".join(sellers) if sellers else "No sellers configured.")
        finally:
            store.close()
    elif args.command == "observe":
        store = Store(config.sqlite_path)
        try:
            if args.observe_command == "add":
                interval = parse_interval(args.interval) if args.interval else None
                if args.interval and interval is None:
                    print("Interval must look like 90s, 3m, or 1h.")
                elif interval is not None:
                    interval = max(config.observe_min_interval_seconds, interval)
                    store.add_observed_seller(args.username, interval)
                    print(f"Observing seller: {args.username} every {format_interval(interval)}")
                else:
                    store.add_observed_seller(args.username)
                    print(f"Observing seller: {args.username}")
            elif args.observe_command == "remove":
                removed = store.remove_observed_seller(args.username)
                print("Removed." if removed else "Seller was not on the observe list.")
            elif args.observe_command == "list":
                rows = store.list_observed_sellers()
                print(format_observed_rows(rows, config.observe_interval_seconds))
        finally:
            store.close()
