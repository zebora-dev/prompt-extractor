"""
dispatch_claude_loop.py — polling dispatch loop for Claude extraction batches.

Polls every 10 minutes:
  1. Counts remaining prompts (optionally filtered by measurements_filter).
  2. Checks for online Prefect workers in the target pool.
  3. Cancels stale / crashed flow runs.
  4. Re-dispatches if prompts remain but no active flows are running.
  5. Scales down and exits once remaining count reaches 0.

Usage:
    python scripts/dispatch_claude_loop.py \\
        --batch-id 45c96267-14f0-40c7-bb1d-5850485cef9f \\
        --measurements-filter Visibility \\
        --region uk \\
        [--worker-count 1] \\
        [--limit 5] \\
        [--poll-interval 600] \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

from automated_extraction.api_client import ApiClient  # noqa: E402
from automated_extraction.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# Prefect helpers
# ---------------------------------------------------------------------------

PREFECT_API_URL = os.getenv("PREFECT_API_URL", "http://localhost:4200/api")

_WORK_POOL_FOR_REGION = {
    "uk": "prompt-extraction-claude-uk",
    "us": "prompt-extraction-claude-us",
}

_FLY_APP_FOR_REGION = {
    "uk": "prompt-extractor-claude-uk",
    "us": "prompt-extractor-claude-us",
}

_DISPATCH_DEPLOYMENT = "dispatch-extraction/dispatch-extraction"


def _prefect_get(path: str) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{PREFECT_API_URL}{path}")
        resp.raise_for_status()
        return resp.json()


def _prefect_post(path: str, body: dict) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(f"{PREFECT_API_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


def count_online_workers(work_pool: str) -> int:
    """Return the number of workers currently online for the given work pool."""
    try:
        data = _prefect_post(f"/work_pools/{work_pool}/workers/filter", {})
        workers = data if isinstance(data, list) else data.get("results", [])
        online = [w for w in workers if w.get("status") == "ONLINE"]
        return len(online)
    except Exception as exc:
        print(f"[warn] Could not fetch workers for pool {work_pool!r}: {exc}")
        return 0


def count_active_flow_runs(deployment_name: str, batch_id: str) -> int:
    """
    Count flow runs for the claude-extraction-batch deployment that are
    currently Running or Pending (not yet Completed/Failed/Cancelled).
    """
    try:
        # Resolve deployment ID
        flow_name, dep_name = deployment_name.split("/", 1)
        resp = _prefect_get(f"/deployments/name/{flow_name}/{dep_name}")
        dep_id = resp["id"]

        body = {
            "deployments": {"id": {"any_": [dep_id]}},
            "flow_runs": {
                "state": {
                    "type": {
                        "any_": ["RUNNING", "PENDING", "SCHEDULED"],
                    }
                }
            },
        }
        data = _prefect_post("/flow_runs/filter", body)
        runs = data if isinstance(data, list) else data.get("results", [])
        # Filter to runs for this specific batch
        matching = [r for r in runs if r.get("parameters", {}).get("batch_id") == batch_id]
        return len(matching)
    except Exception as exc:
        print(f"[warn] Could not count active flow runs: {exc}")
        return 0


def cancel_stale_flow_runs(deployment_name: str, batch_id: str) -> int:
    """Cancel any CRASHED or LATE runs for this batch's deployment."""
    cancelled = 0
    try:
        flow_name, dep_name = deployment_name.split("/", 1)
        resp = _prefect_get(f"/deployments/name/{flow_name}/{dep_name}")
        dep_id = resp["id"]

        body = {
            "deployments": {"id": {"any_": [dep_id]}},
            "flow_runs": {
                "state": {
                    "type": {"any_": ["CRASHED", "FAILED"]},
                }
            },
        }
        data = _prefect_post("/flow_runs/filter", body)
        runs = data if isinstance(data, list) else data.get("results", [])
        stale = [r for r in runs if r.get("parameters", {}).get("batch_id") == batch_id]
        for run in stale:
            try:
                _prefect_post(f"/flow_runs/{run['id']}/set_state", {"state": {"type": "CANCELLED"}})
                print(f"  [cleanup] Cancelled stale run {run['id']} (state={run.get('state', {}).get('type')})")
                cancelled += 1
            except Exception as exc:
                print(f"  [warn] Could not cancel run {run['id']}: {exc}")
    except Exception as exc:
        print(f"[warn] Could not fetch stale runs: {exc}")
    return cancelled


def dispatch_workers(batch_id: str, extraction_type: str, region: str, worker_count: int,
                     limit: int, measurements_filter: str | None) -> list[str]:
    """Submit a dispatch-extraction flow run and return the submitted run ID(s)."""
    try:
        resp = _prefect_get(f"/deployments/name/dispatch-extraction/dispatch-extraction")
        dep_id = resp["id"]
    except Exception as exc:
        print(f"[error] Could not find dispatch-extraction deployment: {exc}")
        return []

    params: dict = {
        "batch_id": batch_id,
        "extraction_type": extraction_type,
        "worker_count": worker_count,
        "region": region,
        "limit": limit,
        "delay_seconds": 30,
        "trigger_scoring": True,
    }
    if measurements_filter:
        params["measurements_filter"] = measurements_filter

    try:
        result = _prefect_post(f"/deployments/{dep_id}/create_flow_run", {"parameters": params})
        run_id = result.get("id", "?")
        print(f"  [dispatch] Submitted dispatch run: {run_id}")
        return [run_id]
    except Exception as exc:
        print(f"[error] Dispatch failed: {exc}")
        return []


def scale_down_fly(region: str, dry_run: bool = False) -> None:
    """Stop the Fly.io worker app for the given region (scale to 0 machines)."""
    app_name = _FLY_APP_FOR_REGION.get(region)
    if not app_name:
        print(f"[warn] Unknown region {region!r}, skipping scale down")
        return
    if dry_run:
        print(f"  [dry-run] Would scale down {app_name}")
        return
    try:
        from automated_extraction.fly_scaler import scale_down
        result = scale_down(app_name=app_name, keep_count=0)
        print(f"  [scale-down] {app_name}: clones_destroyed={len(result.clones_destroyed)} originals_stopped={len(result.originals_stopped)}")
    except Exception as exc:
        print(f"  [warn] Could not scale down {app_name}: {exc}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def make_api_client() -> ApiClient:
    settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)
    return ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )


def run_loop(
    batch_id: str,
    region: str = "uk",
    worker_count: int = 1,
    limit: int = 5,
    measurements_filter: str | None = None,
    poll_interval: int = 600,
    dry_run: bool = False,
) -> None:
    work_pool = _WORK_POOL_FOR_REGION.get(region, f"prompt-extraction-claude-{region}")
    batch_deployment = f"claude-extraction-batch/claude-extraction-batch-{region}" if region != "us" else "claude-extraction-batch/claude-extraction-batch"

    print(f"\n{'='*60}")
    print(f"Claude dispatch loop")
    print(f"  batch_id          : {batch_id}")
    print(f"  region            : {region}")
    print(f"  measurements_filter: {measurements_filter or '(none)'}")
    print(f"  work_pool         : {work_pool}")
    print(f"  worker_count      : {worker_count}")
    print(f"  limit             : {limit}")
    print(f"  poll_interval     : {poll_interval}s")
    print(f"  dry_run           : {dry_run}")
    print(f"{'='*60}\n")

    api = make_api_client()
    batch = api.get_batch(batch_id)
    brand_id = str(batch.get("brand_id") or "")
    if not brand_id:
        raise RuntimeError(f"Batch {batch_id} has no brand_id")
    print(f"Batch resolved: brand_id={brand_id}\n")

    iteration = 0
    while True:
        iteration += 1
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{timestamp}] ── Iteration {iteration} ──")

        # 1. Count remaining prompts
        try:
            remaining = api.get_prompts(
                batch_id,
                brand_id,
                only_remaining=True,
                llm_model_filter="claude",
                measurements_filter=measurements_filter,
            )
            remaining_count = len(remaining)
        except Exception as exc:
            print(f"[error] Could not fetch remaining prompts: {exc}")
            remaining_count = -1

        print(f"  Remaining prompts : {remaining_count}")

        if remaining_count == 0:
            print("\n✓ All prompts complete!")
            print("Scaling down worker app...")
            scale_down_fly(region, dry_run=dry_run)
            print("Loop complete. Exiting.")
            return

        if remaining_count < 0:
            print("  [warn] Could not determine remaining count — will retry next poll.")
        else:
            # 2. Cancel stale flow runs
            stale_cancelled = cancel_stale_flow_runs(batch_deployment, batch_id)
            if stale_cancelled:
                print(f"  Cancelled {stale_cancelled} stale flow run(s).")

            # 3. Check active flow runs
            active_runs = count_active_flow_runs(batch_deployment, batch_id)
            print(f"  Active flow runs  : {active_runs}")

            # 4. Check online workers
            online_workers = count_online_workers(work_pool)
            print(f"  Online workers    : {online_workers}")

            # 5. Re-dispatch if needed
            if active_runs == 0:
                print(f"  No active runs — dispatching {worker_count} worker(s)...")
                if not dry_run:
                    dispatch_workers(
                        batch_id=batch_id,
                        extraction_type="claude",
                        region=region,
                        worker_count=worker_count,
                        limit=limit,
                        measurements_filter=measurements_filter,
                    )
                else:
                    print(f"  [dry-run] Would dispatch {worker_count} worker(s)")
            else:
                print(f"  {active_runs} run(s) already active — skipping dispatch.")

        print(f"  Sleeping {poll_interval}s until next poll...")
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Claude extraction dispatch loop")
    parser.add_argument("--batch-id", required=True, help="Batch ID to process")
    parser.add_argument("--region", default="uk", choices=["uk", "us"], help="Worker region")
    parser.add_argument("--worker-count", type=int, default=1, help="Workers to dispatch")
    parser.add_argument("--limit", type=int, default=5, help="Prompts per inner run")
    parser.add_argument("--measurements-filter", default=None, help="e.g. 'Visibility'")
    parser.add_argument("--poll-interval", type=int, default=600, help="Seconds between polls (default 600)")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not dispatch or scale down")
    args = parser.parse_args()

    run_loop(
        batch_id=args.batch_id,
        region=args.region,
        worker_count=args.worker_count,
        limit=args.limit,
        measurements_filter=args.measurements_filter,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
