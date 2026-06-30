#!/usr/bin/env python3
"""Small Prefect HTTP API helper for the BrandSight dispatch skill."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


DEFAULT_API = "https://prompt-extractor-prefect.fly.dev/api"


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} from {url}: {body}") from exc
    if not body:
        return None
    return json.loads(body)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def command_deployment_id(args: argparse.Namespace) -> int:
    flow_name = urllib.parse.quote(args.flow_name, safe="")
    deployment_name = urllib.parse.quote(args.deployment_name, safe="")
    url = f"{args.api}/deployments/name/{flow_name}/{deployment_name}"
    deployment = request_json("GET", url)
    print(deployment.get("id", "NOT FOUND"))
    return 0


def command_flow_runs(args: argparse.Namespace) -> int:
    tracked = {item for item in (args.tracked or "").split(",") if item}
    payload = {
        "deployments": {"id": {"any_": [args.deployment_id]}},
        "sort": "START_TIME_DESC",
        "limit": args.limit,
    }
    runs = request_json("POST", f"{args.api}/flow_runs/filter", payload)
    now = datetime.now(timezone.utc)
    live = []
    dropped = []
    for run in runs:
        state = run.get("state", {}).get("type", "UNKNOWN")
        run_id = run.get("id")
        created = parse_dt(run.get("created"))
        age_min = None if created is None else round((now - created).total_seconds() / 60, 1)
        row = {
            "id": run_id,
            "state": state,
            "age_min": age_min,
            "name": run.get("name"),
        }
        if state in {"RUNNING", "SCHEDULED", "PENDING"}:
            live.append(row)
        elif run_id in tracked and state in {"CANCELLED", "COMPLETED", "FAILED", "CRASHED"}:
            dropped.append(row)
    print(json.dumps({"live": live, "dropped": dropped}, indent=2, sort_keys=True))
    return 0


def command_workers(args: argparse.Namespace) -> int:
    payload = {"workers": {"work_pool_name": {"any_": [args.work_pool]}}, "limit": args.limit}
    workers = request_json("POST", f"{args.api}/workers/filter", payload)
    now = datetime.now(timezone.utc)
    rows = []
    for worker in workers:
        last = parse_dt(worker.get("last_heartbeat_time"))
        age_seconds = None if last is None else int((now - last).total_seconds())
        rows.append(
            {
                "name": worker.get("name"),
                "status": worker.get("status"),
                "last_heartbeat_age_seconds": age_seconds,
                "stale": age_seconds is not None and age_seconds > 600,
            }
        )
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def command_create_flow_run(args: argparse.Namespace) -> int:
    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params-json is not valid JSON: {exc}") from exc
    payload = {"parameters": params}
    run = request_json(
        "POST",
        f"{args.api}/deployments/{args.deployment_id}/create_flow_run",
        payload,
    )
    print(json.dumps(run, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    subparsers = parser.add_subparsers(dest="command", required=True)

    deployment_id = subparsers.add_parser("deployment-id")
    deployment_id.add_argument("--flow-name", required=True)
    deployment_id.add_argument("--deployment-name", required=True)
    deployment_id.set_defaults(func=command_deployment_id)

    flow_runs = subparsers.add_parser("flow-runs")
    flow_runs.add_argument("--deployment-id", required=True)
    flow_runs.add_argument("--tracked")
    flow_runs.add_argument("--limit", type=int, default=15)
    flow_runs.set_defaults(func=command_flow_runs)

    workers = subparsers.add_parser("workers")
    workers.add_argument("--work-pool", required=True)
    workers.add_argument("--limit", type=int, default=10)
    workers.set_defaults(func=command_workers)

    create_flow_run = subparsers.add_parser("create-flow-run")
    create_flow_run.add_argument("--deployment-id", required=True)
    create_flow_run.add_argument("--params-json", required=True)
    create_flow_run.set_defaults(func=command_create_flow_run)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
