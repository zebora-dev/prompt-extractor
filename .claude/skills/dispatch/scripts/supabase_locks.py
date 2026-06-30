#!/usr/bin/env python3
"""Inspect and optionally release stale GPT-UK Supabase profile locks."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any


DEFAULT_URL = "https://hmwgplzdzffivawkflci.supabase.co"


def load_dotenv() -> None:
    try:
        from dotenv import load_dotenv as real_load_dotenv
    except ImportError:
        return
    real_load_dotenv()


def supabase_client() -> Any:
    load_dotenv()
    try:
        from supabase import create_client
    except ImportError as exc:
        raise SystemExit("Install supabase-py or run through the project environment") from exc

    url = os.environ.get("BRANDSIGHT_SUPABASE_URL") or DEFAULT_URL
    key = os.environ.get("BRANDSIGHT_SUPABASE_SERVICE_KEY")
    if not key:
        raise SystemExit("BRANDSIGHT_SUPABASE_SERVICE_KEY is required")
    return create_client(url, key)


def parse_machines(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def command_stale(args: argparse.Namespace) -> int:
    active_machines = parse_machines(args.active_machines)
    client = supabase_client()
    result = (
        client.table("chatgpt_profiles")
        .select('"index", email, locked_by, locked_at')
        .eq("is_locked", True)
        .neq("locked_by", "disabled")
        .execute()
    )
    stale = [row for row in result.data if row.get("locked_by") not in active_machines]
    if not stale:
        print("No stale locks found.")
        return 0

    for row in stale:
        print(
            "STALE "
            f"index={row.get('index')} "
            f"email={row.get('email')} "
            f"locked_by={row.get('locked_by')} "
            f"locked_at={row.get('locked_at')}"
        )

    if not args.apply:
        print("DRY RUN: add --apply to release these locks.")
        return 0

    stale_indices = [row["index"] for row in stale]
    (
        client.table("chatgpt_profiles")
        .update(
            {
                "is_locked": False,
                "locked_by": None,
                "locked_at": None,
                "lock_expires_at": None,
            }
        )
        .in_('"index"', stale_indices)
        .execute()
    )
    for row in stale:
        print(
            "Released stale lock: "
            f"index={row.get('index')} email={row.get('email')} "
            f"was held by {row.get('locked_by')}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stale = subparsers.add_parser("stale")
    stale.add_argument("--active-machines", required=True)
    stale.add_argument("--apply", action="store_true")
    stale.set_defaults(func=command_stale)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
