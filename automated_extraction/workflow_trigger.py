from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoreWorkflowTriggerResult:
    status: str
    attempted_count: int
    triggered_count: int
    skipped_count: int
    failed_count: int
    failures: list[dict[str, Any]]


def trigger_score_workflows(
    *,
    settings: Settings,
    saved_outputs: list[dict[str, Any]] | None = None,
    force: bool = False,
    force_run: bool | None = None,
    scorer_types: list[str] | None = None,
    max_retries: int = 2,
) -> ScoreWorkflowTriggerResult:
    outputs = saved_outputs or []
    attempted_count = 0
    triggered_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []

    for output in outputs:
        output_id = output.get("output_id") or output.get("id") or output.get("prompt_output_id")
        batch_id = output.get("batch_id")
        prompt_id = output.get("prompt_id")
        if not output_id or not batch_id:
            skipped_count += 1
            LOGGER.warning(
                "Skipping score workflow trigger because output ref is missing output_id or batch_id. output_id=%s batch_id=%s prompt_id=%s",
                output_id,
                batch_id,
                prompt_id,
            )
            continue

        attempted_count += 1
        try:
            parsed_output_id = parse_output_id(output_id)
            trigger_single_score_workflow(
                workflow_url=settings.score_workflow_url,
                workflow_api_key=settings.workflow_api_key,
                batch_id=str(batch_id),
                output_id=str(parsed_output_id),
                force=force,
                force_run=settings.score_workflow_force_run if force_run is None else force_run,
                scorer_types=settings.score_workflow_scorer_types if scorer_types is None else scorer_types,
                max_retries=max_retries,
            )
            triggered_count += 1
            LOGGER.info("Triggered score workflow. batch_id=%s output_id=%s force=%s", batch_id, parsed_output_id, force)
        except Exception as exc:
            failed_count += 1
            failure = {"batch_id": batch_id, "output_id": output_id, "prompt_id": prompt_id, "error": str(exc)}
            failures.append(failure)
            LOGGER.exception("Failed to trigger score workflow. batch_id=%s output_id=%s: %s", batch_id, output_id, exc)

    status = "completed" if failed_count == 0 else "completed_with_failures"
    if attempted_count == 0 and skipped_count == 0:
        status = "no_outputs"
    return ScoreWorkflowTriggerResult(
        status=status,
        attempted_count=attempted_count,
        triggered_count=triggered_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        failures=failures,
    )


def trigger_single_score_workflow(
    *,
    workflow_url: str,
    workflow_api_key: str | None,
    batch_id: str,
    output_id: str,
    force: bool,
    force_run: bool,
    scorer_types: list[str],
    max_retries: int,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if workflow_api_key:
        headers["X-API-Key"] = workflow_api_key

    payload = {
        "batch_id": batch_id,
        "output_id": output_id,
        "force": force,
        "force_run": force_run,
    }
    if scorer_types:
        payload["scorer_types"] = scorer_types
    for attempt in range(max_retries + 1):
        response = requests.post(workflow_url, headers=headers, json=payload, timeout=60)
        if response.status_code >= 500 and attempt < max_retries:
            time.sleep(min(30, 2**attempt))
            continue
        if response.status_code == 429 and attempt < max_retries:
            time.sleep(retry_after_seconds(response) or min(30, 2**attempt))
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Score workflow trigger failed ({response.status_code}): {response.text}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {"raw_response": response.text}
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"Score workflow trigger failed: {data.get('error') or data}")
        return data if isinstance(data, dict) else {"data": data}

    raise RuntimeError("Score workflow trigger failed after retries")


def parse_output_id(output_id: Any) -> int:
    try:
        return int(output_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid prompt output id for score workflow trigger: {output_id!r}") from exc


def retry_after_seconds(response: requests.Response) -> int | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0, int(header))
    except ValueError:
        return None
