from __future__ import annotations

import argparse
import asyncio
import logging

from .config import Config
from .service import EbaySpyService
from .storage import Store, format_status_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebayspy")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="Run the hourly Telegram tracker")
    subparsers.add_parser("check", help="Run one poll immediately")
    subparsers.add_parser("status", help="Show the last check status for each seller")

    sellers = subparsers.add_parser("sellers", help="Manage watched sellers")
    seller_sub = sellers.add_subparsers(dest="seller_command", required=True)
    add = seller_sub.add_parser("add", help="Add a seller")
    add.add_argument("username")
    remove = seller_sub.add_parser("remove", help="Remove a seller")
    remove.add_argument("username")
    seller_sub.add_parser("list", help="List sellers")
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
