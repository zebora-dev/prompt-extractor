"""
Dispatcher flow — automatically distributes a batch across N workers.

Rather than manually calculating offsets and triggering N separate batch
runs, one call to dispatch_extraction_flow:

  1. Queries the remaining prompt count for the batch.
  2. Divides the work into equal chunks — one per worker.
  3. Submits N batch flow runs via the Prefect REST API and exits immediately.

Workers then run autonomously, each processing its assigned chunk.  The
work pool's concurrency limit (set once per pool, not per dispatch) ensures
no more flows run simultaneously than there are live worker machines.

Supported extraction types
--------------------------
  "google-ai-overview"   →  google-ai-overview-extraction-batch deployment
  "google-ai-mode"       →  google-ai-mode-extraction-batch deployment
  "chatgpt"              →  prompt-extraction-batch deployment

Scaling
-------
Adding workers: increase the work pool concurrency limit and pass a higher
worker_count to this flow.  No other changes needed.
"""
from __future__ import annotations

import math
import os
from typing import Any

import httpx
from prefect import flow
from prefect.logging import get_run_logger

from automated_extraction.api_client import ApiClient
from automated_extraction.config import Settings

# ---------------------------------------------------------------------------
# Extraction-type metadata
# ---------------------------------------------------------------------------

_EXTRACTION_TYPES: dict[str, dict[str, str]] = {
    "google-ai-overview": {
        "flow_name": "google-ai-overview-extraction-batch",
        "deployment_base": "google-ai-overview-extraction-batch",
        "model_filter": "google-ai-overview",
    },
    "google-ai-mode": {
        "flow_name": "google-ai-mode-extraction-batch",
        "deployment_base": "google-ai-mode-extraction-batch",
        "model_filter": "google-ai-mode",
    },
    "chatgpt": {
        "flow_name": "prompt-extraction-batch",
        "deployment_base": "prompt-extraction-batch",
        "model_filter": "gpt",
    },
}

_REGION_SUFFIXES: dict[str, str] = {
    "us": "",
    "uk": "-uk",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_api_client() -> ApiClient:
    settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)
    return ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )


def _get_remaining_count(
    api: ApiClient,
    batch_id: str,
    brand_id: str,
    model_filter: str | None,
) -> int:
    prompts = api.get_prompts(
        batch_id,
        brand_id,
        only_remaining=True,
        llm_model_filter=model_filter,
    )
    return len(prompts)


def _submit_worker_run(
    prefect_api_url: str,
    deployment_full_name: str,
    parameters: dict[str, Any],
) -> str:
    """
    Submit a Prefect flow run from a deployment via the REST API and return
    the new flow run ID.  Uses httpx (sync) to avoid async/event-loop
    complexity inside a sync Prefect flow.

    deployment_full_name format: "<flow-name>/<deployment-name>"
    e.g. "google-ai-overview-extraction-batch/google-ai-overview-extraction-batch-uk"
    """
    flow_name, deployment_name = deployment_full_name.split("/", 1)

    with httpx.Client(timeout=30.0) as client:
        # Resolve deployment ID by name
        resp = client.get(
            f"{prefect_api_url}/deployments/name/{flow_name}/{deployment_name}",
        )
        if resp.status_code == 404:
            raise RuntimeError(
                f"Deployment not found: {deployment_full_name!r}. "
                "Make sure it is registered for this region."
            )
        resp.raise_for_status()
        deployment_id = resp.json()["id"]

        # Create the flow run
        resp = client.post(
            f"{prefect_api_url}/deployments/{deployment_id}/create_flow_run",
            json={"parameters": parameters},
        )
        resp.raise_for_status()
        return resp.json()["id"]


# ---------------------------------------------------------------------------
# Dispatcher flow
# ---------------------------------------------------------------------------

@flow(
    name="dispatch-extraction",
    flow_run_name="dispatch-extraction-{extraction_type}-{batch_id}",
    log_prints=True,
)
def dispatch_extraction_flow(
    batch_id: str | None = None,
    extraction_type: str | None = None,
    worker_count: int = 4,
    region: str = "uk",
    # Inner-run tuning (passed through to each batch flow)
    limit: int = 5,
    delay_seconds: int = 60,
    # Google-specific
    use_proxy: bool = False,
    country: str | None = None,
    language: str | None = None,
    # ChatGPT-specific
    auto_login: bool = False,
    login_email: str | None = None,
    capture_products: bool = False,
    capture_entities: bool = False,
    # Dynamic scaling
    auto_scale: bool = False,
    scale_wait_seconds: int = 30,
    # Worker staggering
    stagger_seconds: int = 15,
) -> dict[str, Any]:
    """
    Dispatch extraction work across N workers automatically.

    Queries the remaining prompt count, splits it into equal chunks, and
    submits one batch flow run per worker.  Exits immediately after scheduling
    — workers run autonomously.

    Parameters
    ----------
    batch_id          : ID of the batch to process.
    extraction_type   : One of "google-ai-overview", "google-ai-mode", "chatgpt".
    worker_count      : Number of workers to distribute work across.
    region            : "us" or "uk" — selects the regional deployment variant.
    limit             : Prompts per inner extraction run (rate-limiting knob).
    delay_seconds     : Seconds to pause between inner runs on each worker.
    use_proxy         : (Google) Route Chrome through the regional proxy.
    country/language  : (Google) Override geo-targeting.
    auto_login        : (ChatGPT) Enable automated login.
    login_email       : (ChatGPT) Account to use for auto-login.
    capture_products  : (ChatGPT) Extract product entities from responses.
    capture_entities  : (ChatGPT) Extract named entities from responses.
    auto_scale        : If True, automatically scale Fly.io machines to
                        match worker_count before submitting flows.
                        Requires FLY_API_TOKEN secret to be set on the app.
    scale_wait_seconds: Seconds to wait after scaling for new Prefect workers
                        to connect before flows are submitted (default 30).
    stagger_seconds   : Per-worker startup delay. Worker i sleeps i *
                        stagger_seconds before launching Chrome. Spreads 20
                        workers over ~5 min at the default of 15s, preventing
                        simultaneous Google requests that trigger rate-limiting.
                        Set to 0 to disable staggering.
    """
    flow_logger = get_run_logger()

    # -- Validate inputs -------------------------------------------------------
    if not batch_id:
        raise ValueError("batch_id is required")
    if not extraction_type:
        raise ValueError("extraction_type is required")
    if extraction_type not in _EXTRACTION_TYPES:
        raise ValueError(
            f"Unknown extraction_type {extraction_type!r}. "
            f"Choose from: {list(_EXTRACTION_TYPES)}"
        )
    if region not in _REGION_SUFFIXES:
        raise ValueError(
            f"Unknown region {region!r}. Choose from: {list(_REGION_SUFFIXES)}"
        )
    if worker_count < 1:
        raise ValueError("worker_count must be >= 1")

    type_meta = _EXTRACTION_TYPES[extraction_type]
    region_suffix = _REGION_SUFFIXES[region]
    model_filter = type_meta["model_filter"]
    deployment_full_name = (
        f"{type_meta['flow_name']}"
        f"/{type_meta['deployment_base']}{region_suffix}"
    )
    prefect_api_url = os.getenv("PREFECT_API_URL", "http://localhost:4200/api")

    # -- Count remaining prompts -----------------------------------------------
    api = _make_api_client()
    batch = api.get_batch(batch_id)
    brand_id = str(batch.get("brand_id") or "")
    if not brand_id:
        raise RuntimeError(f"Batch {batch_id} has no brand_id")

    remaining_count = _get_remaining_count(api, batch_id, brand_id, model_filter)
    if remaining_count == 0:
        flow_logger.info(
            "No remaining prompts for batch %s (extraction_type=%s). Nothing to dispatch.",
            batch_id, extraction_type,
        )
        return {
            "status": "nothing_to_dispatch",
            "batch_id": batch_id,
            "extraction_type": extraction_type,
            "remaining_count": 0,
            "workers_dispatched": 0,
        }

    # -- Calculate chunks -------------------------------------------------------
    # Cap worker_count so we never dispatch more flows than there are prompts.
    effective_workers = min(worker_count, remaining_count)
    chunk_size = math.ceil(remaining_count / effective_workers)

    flow_logger.info(
        "Dispatching %s worker(s) for batch %s. extraction_type=%s region=%s "
        "remaining=%s chunk_size=%s limit_per_run=%s deployment=%s",
        effective_workers, batch_id, extraction_type, region,
        remaining_count, chunk_size, limit, deployment_full_name,
    )

    # -- Auto-scale Fly.io machines to match effective_workers -----------------
    scale_result: dict[str, Any] | None = None
    if auto_scale:
        try:
            from automated_extraction.fly_scaler import app_name_for_region, scale_up
            fly_app = app_name_for_region(region)
            work_pool_name = os.getenv("PREFECT_WORK_POOL", f"prompt-extraction-{region}")
            flow_logger.info(
                "auto_scale=True: scaling %s up to %d machines (pool=%s, wait=%ds)",
                fly_app, effective_workers, work_pool_name, scale_wait_seconds,
            )
            result_obj = scale_up(
                app_name=fly_app,
                target_count=effective_workers,
                prefect_api_url=prefect_api_url,
                work_pool=work_pool_name,
                wait_for_workers_seconds=scale_wait_seconds,
            )
            scale_result = result_obj.to_dict()
            flow_logger.info("Scale-up result: %s", scale_result)
        except Exception as exc:
            flow_logger.warning(
                "auto_scale failed (continuing without scaling): %s", exc,
            )

    # -- Build per-worker parameters -------------------------------------------
    base_params: dict[str, Any] = {
        "batch_id": batch_id,
        "limit": limit,
        "delay_seconds": delay_seconds,
    }

    if extraction_type in ("google-ai-overview", "google-ai-mode"):
        base_params.update({
            "model_filter": model_filter,
            "use_proxy": use_proxy,
            "country": country,
            "language": language,
        })
    else:  # chatgpt
        base_params.update({
            "model_filter": model_filter,
            "auto_login": auto_login,
            "login_email": login_email,
            "capture_products": capture_products,
            "capture_entities": capture_entities,
        })

    # -- Submit one flow run per worker ----------------------------------------
    submitted: list[dict[str, Any]] = []
    for i in range(effective_workers):
        skip = i * chunk_size
        worker_params = {
            **base_params,
            "skip": skip,
            "max_prompts": chunk_size,
            "startup_delay_seconds": i * stagger_seconds,
        }
        try:
            run_id = _submit_worker_run(prefect_api_url, deployment_full_name, worker_params)
            flow_logger.info(
                "Submitted worker %s/%s — flow_run_id=%s skip=%s max_prompts=%s",
                i + 1, effective_workers, run_id, skip, chunk_size,
            )
            submitted.append({
                "worker_index": i + 1,
                "flow_run_id": run_id,
                "skip": skip,
                "max_prompts": chunk_size,
            })
        except Exception as exc:
            flow_logger.error(
                "Failed to submit worker %s/%s (skip=%s): %s",
                i + 1, effective_workers, skip, exc,
            )
            submitted.append({
                "worker_index": i + 1,
                "flow_run_id": None,
                "skip": skip,
                "max_prompts": chunk_size,
                "error": str(exc),
            })

    failed_submissions = [s for s in submitted if s.get("error")]
    summary = {
        "status": "dispatched" if not failed_submissions else "dispatched_with_errors",
        "batch_id": batch_id,
        "extraction_type": extraction_type,
        "region": region,
        "deployment": deployment_full_name,
        "remaining_count": remaining_count,
        "worker_count": worker_count,
        "effective_workers": effective_workers,
        "chunk_size": chunk_size,
        "limit_per_run": limit,
        "workers": submitted,
        "scale_result": scale_result,
    }
    flow_logger.info("Dispatch complete: %s", summary)
    return summary
