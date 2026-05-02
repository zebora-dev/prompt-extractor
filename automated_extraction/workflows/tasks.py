from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from prefect import task
from prefect.logging import get_run_logger

from automated_extraction.config import Settings
from automated_extraction.extraction import run_extraction_job
from automated_extraction.prompt_output_processor import process_prompt_outputs


LOGGER = logging.getLogger(__name__)


def _extract_batch_task_run_name() -> str:
    try:
        from prefect.runtime import task_run

        params = getattr(task_run, "parameters", None) or {}
        batch_id = params.get("batch_id") or "local-prompts"
        return f"extract-chatgpt-{batch_id}"
    except Exception as exc:
        LOGGER.debug("Prefect runtime not available: %s", exc)
        return "extract-chatgpt-batch"


@task(
    name="extract-chatgpt-batch",
    task_run_name=_extract_batch_task_run_name,
    retries=0,
    timeout_seconds=None,
    tags=["chatgpt", "extraction", "browser"],
    cache_result_in_memory=False,
)
def extract_chatgpt_batch_task(
    *,
    batch_id: str | None = None,
    prompts_file: str | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    sources_panel_pause_seconds: int = 0,
) -> dict[str, Any]:
    """
    Run the current CLI extraction process as one observable Prefect task.

    Keeping this as one task preserves the single logged-in browser lifecycle
    while still giving Prefect run state, logs, parameters, and retries.
    """
    task_logger = get_run_logger()
    task_logger.info(
        "Starting ChatGPT extraction task. batch_id=%s prompts_file=%s limit=%s skip=%s dry_run=%s",
        batch_id,
        prompts_file,
        limit,
        skip,
        dry_run,
    )
    settings = Settings.from_env(require_api_key=True)
    result = run_extraction_job(
        settings=settings,
        batch_id=batch_id,
        prompts_file=Path(prompts_file) if prompts_file else None,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        sources_panel_pause_seconds=sources_panel_pause_seconds,
    )
    payload = asdict(result)
    task_logger.info("Finished ChatGPT extraction task: %s", payload)
    if result.failed_count:
        task_logger.warning("Extraction completed with %s failed prompt(s).", result.failed_count)
    return payload


def _process_outputs_task_run_name() -> str:
    try:
        from prefect.runtime import task_run

        params = getattr(task_run, "parameters", None) or {}
        batch_id = params.get("batch_id") or "latest"
        return f"process-prompt-outputs-{batch_id}"
    except Exception as exc:
        LOGGER.debug("Prefect runtime not available: %s", exc)
        return "prompt-output-process"


@task(
    name="prompt-output-process",
    task_run_name=_process_outputs_task_run_name,
    retries=0,
    timeout_seconds=None,
    tags=["prompt-output", "post-process", "markdown"],
    cache_result_in_memory=False,
)
def prompt_output_process_task(
    *,
    saved_outputs: list[dict[str, Any]] | None = None,
    output_id: int | str | None = None,
    batch_id: str | None = None,
    brand_id: str | None = None,
    prompt_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Re-process saved prompt outputs after extraction.

    The processor converts captured raw HTML into markdown, compares it with
    the copied markdown, then enriches the saved response/markdown with any
    assets or links that the copy button omitted.
    """
    task_logger = get_run_logger()
    task_logger.info(
        "Starting prompt output process task. saved_outputs=%s output_id=%s batch_id=%s brand_id=%s prompt_id=%s limit=%s",
        len(saved_outputs or []),
        output_id,
        batch_id,
        brand_id,
        prompt_id,
        limit,
    )
    settings = Settings.from_env(require_api_key=True)
    result = process_prompt_outputs(
        settings=settings,
        saved_outputs=saved_outputs,
        output_id=output_id,
        batch_id=batch_id,
        brand_id=brand_id,
        prompt_id=prompt_id,
        limit=limit,
    )
    payload = asdict(result)
    task_logger.info("Finished prompt output process task: %s", payload)
    if result.failed_count:
        task_logger.warning("Prompt output processing completed with %s failure(s).", result.failed_count)
    return payload
