"""
Prefect flows for dynamically scaling Fly.io Prefect worker machines.

Two flows are registered as Prefect deployments:

  scale-workers
    Scale up the Fly.io machines for a given region to `target_count` running
    instances.  Starts stopped originals first, clones new machines for the
    remainder, then updates the Prefect work-pool concurrency limit.

  scale-workers-down
    Destroy all cloned machines and stop original machines above `keep_count`.
    Resets the work-pool concurrency limit to match.

Both flows are thin wrappers around `automated_extraction.fly_scaler` which
holds all the Machines API logic.
"""

from __future__ import annotations

import os
from typing import Any

from prefect import flow
from prefect.logging import get_run_logger

from automated_extraction.fly_scaler import (
    ScaleDownResult,
    ScaleResult,
    app_name_for_region,
    scale_down,
    scale_up,
)


def _prefect_api_url() -> str:
    return os.getenv("PREFECT_API_URL", "http://localhost:4200/api")


def _work_pool_for_region(region: str, work_pool: str | None) -> str:
    if work_pool:
        return work_pool
    suffix = "" if region.lower() == "us" else f"-{region.lower()}"
    env_pool = os.getenv("PREFECT_WORK_POOL", "prompt-extraction-pool")
    return env_pool if suffix == "" else f"prompt-extraction{suffix}"


@flow(
    name="scale-workers",
    flow_run_name="scale-workers-{region}-to-{target_count}",
    log_prints=True,
)
def scale_workers_flow(
    target_count: int = 4,
    region: str = "uk",
    work_pool: str | None = None,
    wait_for_workers_seconds: int = 30,
) -> dict[str, Any]:
    """
    Scale up Fly.io worker machines to `target_count`.

    Parameters
    ----------
    target_count          : Desired number of running machines (1–20).
    region                : "uk" or "us" — selects the Fly.io app.
    work_pool             : Prefect work pool to update concurrency on.
                            Defaults to prompt-extraction-{region}.
    wait_for_workers_seconds : Seconds to pause after start/clone so newly
                            booted Prefect workers have time to connect.
    """
    logger = get_run_logger()

    resolved_app = app_name_for_region(region)
    resolved_pool = _work_pool_for_region(region, work_pool)
    prefect_url = _prefect_api_url()

    logger.info(
        "Scaling %s up to %d machines (work_pool=%s)",
        resolved_app,
        target_count,
        resolved_pool,
    )

    result: ScaleResult = scale_up(
        app_name=resolved_app,
        target_count=target_count,
        prefect_api_url=prefect_url,
        work_pool=resolved_pool,
        wait_for_workers_seconds=wait_for_workers_seconds,
    )

    summary = result.to_dict()
    logger.info("Scale-up complete: %s", summary)
    return summary


@flow(
    name="scale-workers-down",
    flow_run_name="scale-workers-down-{region}-keep-{keep_count}",
    log_prints=True,
)
def scale_workers_down_flow(
    region: str = "uk",
    keep_count: int = 1,
    work_pool: str | None = None,
) -> dict[str, Any]:
    """
    Scale down Fly.io worker machines.

    Destroys all cloned machines and stops original machines above `keep_count`.
    Resets the Prefect work-pool concurrency limit to `keep_count`.

    Parameters
    ----------
    region      : "uk" or "us".
    keep_count  : Number of original machines to leave running (default 1).
    work_pool   : Prefect work pool whose concurrency limit to reset.
    """
    logger = get_run_logger()

    resolved_app = app_name_for_region(region)
    resolved_pool = _work_pool_for_region(region, work_pool)
    prefect_url = _prefect_api_url()

    logger.info(
        "Scaling %s down to %d machines (work_pool=%s)",
        resolved_app,
        keep_count,
        resolved_pool,
    )

    result: ScaleDownResult = scale_down(
        app_name=resolved_app,
        keep_count=keep_count,
        prefect_api_url=prefect_url,
        work_pool=resolved_pool,
    )

    summary = result.to_dict()
    logger.info("Scale-down complete: %s", summary)
    return summary
