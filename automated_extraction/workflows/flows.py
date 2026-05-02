from __future__ import annotations

from typing import Any

from prefect import flow
from prefect.logging import get_run_logger

from automated_extraction.workflows.tasks import extract_chatgpt_batch_task, prompt_output_process_task


@flow(
    name="prompt-extraction",
    flow_run_name="prompt-extraction-{batch_id}",
    log_prints=True,
)
def prompt_extraction_flow(
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
    Orchestrate a ChatGPT prompt extraction run.

    This flow intentionally wraps the existing browser-based extraction as a
    single task so one Chrome session can process many prompts.
    """
    flow_logger = get_run_logger()
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    flow_logger.info(
        "Starting prompt extraction flow. batch_id=%s prompts_file=%s brand_id=%s limit=%s skip=%s",
        batch_id,
        prompts_file,
        brand_id,
        limit,
        skip,
    )
    result = extract_chatgpt_batch_task(
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        sources_panel_pause_seconds=sources_panel_pause_seconds,
    )
    processing_result: dict[str, Any] | None = None
    if not dry_run and result.get("saved_count", 0) > 0:
        processing_result = prompt_output_process_task(
            saved_outputs=result.get("saved_outputs") or [],
            batch_id=result.get("batch_id") or batch_id,
            brand_id=result.get("brand_id") or brand_id,
            limit=result.get("saved_count") or limit or 50,
        )
    else:
        flow_logger.info("Skipping prompt output processing because no outputs were saved.")

    combined_result = {
        **result,
        "prompt_output_processing": processing_result,
    }
    flow_logger.info("Prompt extraction flow finished: %s", combined_result)
    return combined_result


@flow(
    name="prompt-output-processing",
    flow_run_name="prompt-output-processing-{output_id}-{batch_id}",
    log_prints=True,
)
def prompt_output_processing_flow(
    output_id: int | str | None = None,
    batch_id: str | None = None,
    brand_id: str | None = None,
    prompt_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Re-process existing saved prompt outputs without running ChatGPT extraction.
    """
    flow_logger = get_run_logger()
    if not output_id and not batch_id and not prompt_id:
        raise ValueError("one of output_id, batch_id, or prompt_id is required")

    flow_logger.info(
        "Starting prompt output processing flow. output_id=%s batch_id=%s brand_id=%s prompt_id=%s limit=%s",
        output_id,
        batch_id,
        brand_id,
        prompt_id,
        limit,
    )
    result = prompt_output_process_task(
        output_id=output_id,
        batch_id=batch_id,
        brand_id=brand_id,
        prompt_id=prompt_id,
        limit=limit,
    )
    flow_logger.info("Prompt output processing flow finished: %s", result)
    return result
