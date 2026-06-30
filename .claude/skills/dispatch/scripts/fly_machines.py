#!/usr/bin/env python3
"""Fly machine helper with dry-run defaults for mutating actions."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time


def fly_binary() -> str:
    found = shutil.which("flyctl") or shutil.which("fly")
    if not found:
        raise SystemExit("Neither flyctl nor fly is available on PATH")
    return found


def run(command: list[str], apply: bool) -> None:
    print("+ " + " ".join(command))
    if apply:
        subprocess.run(command, check=True)


def parse_machines(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def command_list(args: argparse.Namespace) -> int:
    binary = fly_binary()
    output = subprocess.check_output(
        [binary, "machines", "list", "-a", args.app, "--json"],
        text=True,
    )
    machines = json.loads(output)
    for machine in machines:
        email = machine.get("config", {}).get("env", {}).get("CHATGPT_LOGIN_EMAIL", "")
        print(f"{machine.get('id')}\t{machine.get('state')}\t{email}")
    print(f"Total: {len(machines)} machines")
    return 0


def command_mutate(args: argparse.Namespace) -> int:
    binary = fly_binary()
    machines = parse_machines(args.machines)
    if not args.apply:
        print("DRY RUN: add --apply to execute these machine actions.")
    for machine in machines:
        if args.command == "cycle":
            run([binary, "machines", "stop", machine, "-a", args.app], args.apply)
            if args.apply:
                time.sleep(args.stop_wait_seconds)
            run([binary, "machines", "start", machine, "-a", args.app], args.apply)
            if args.apply:
                time.sleep(args.start_wait_seconds)
        else:
            run([binary, "machines", args.command, machine, "-a", args.app], args.apply)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--app", required=True)
    list_parser.set_defaults(func=command_list)

    for command in ("start", "stop", "restart", "cycle"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--app", required=True)
        sub.add_argument("--machines", required=True)
        sub.add_argument("--apply", action="store_true")
        sub.add_argument("--stop-wait-seconds", type=int, default=5)
        sub.add_argument("--start-wait-seconds", type=int, default=15)
        sub.set_defaults(func=command_mutate)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
