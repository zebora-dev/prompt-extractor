from __future__ import annotations

import time
from typing import Any

from prefect import flow
from prefect.logging import get_run_logger

from automated_extraction.workflows.tasks import (
    entity_output_process_task,
    extract_chatgpt_batch_task,
    product_output_process_task,
    prompt_output_process_task,
    score_workflow_trigger_task,
)


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
    force_rerun: bool = False,
    llm_model_filter: str | None = "gpt",
    auto_login: bool | None = None,
    login_email: str | None = None,
    capture_products: bool = False,
    capture_entities: bool = False,
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
        "Starting prompt extraction flow. batch_id=%s prompts_file=%s brand_id=%s limit=%s skip=%s force_rerun=%s llm_model_filter=%s auto_login=%s login_email=%s capture_products=%s capture_entities=%s",
        batch_id,
        prompts_file,
        brand_id,
        limit,
        skip,
        force_rerun,
        llm_model_filter or "any",
        auto_login,
        login_email or "<env>",
        capture_products,
        capture_entities,
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
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        auto_login=auto_login,
        login_email=login_email,
        capture_products=capture_products,
        capture_entities=capture_entities,
    )
    product_output_refs = result.pop("product_outputs", []) or []
    entity_output_refs = result.pop("entity_outputs", []) or []

    product_processing_result: dict[str, Any] | None = None
    if not dry_run and capture_products and product_output_refs:
        product_processing_result = product_output_process_task(product_output_refs=product_output_refs)
    else:
        flow_logger.info("Skipping product output processing. capture_products=%s refs=%s", capture_products, len(product_output_refs))

    entity_processing_result: dict[str, Any] | None = None
    if not dry_run and capture_entities and entity_output_refs:
        entity_processing_result = entity_output_process_task(entity_output_refs=entity_output_refs)
    else:
        flow_logger.info("Skipping entity output processing. capture_entities=%s refs=%s", capture_entities, len(entity_output_refs))

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

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(saved_outputs=result.get("saved_outputs") or [], force=False)
    else:
        flow_logger.info("Skipping score workflow trigger because no outputs were saved.")

    combined_result = {
        **result,
        "product_output_processing": product_processing_result,
        "entity_output_processing": entity_processing_result,
        "prompt_output_processing": processing_result,
        "score_workflow_trigger": score_workflow_result,
    }
    flow_logger.info("Prompt extraction flow finished: %s", combined_result)
    return combined_result


@flow(
    name="batch-extraction",
    log_prints=True,
)
def batch_extraction_flow(
    batch_ids: list[str] | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    force_rerun: bool = False,
    llm_model_filter: str | None = "gpt",
    auto_login: bool | None = None,
    login_email: str | None = None,
    capture_products: bool = False,
    capture_entities: bool = False,
    delay_seconds: int = 120,
) -> dict[str, Any]:
    """
    Run prompt-extraction sequentially for a list of batch IDs, with a
    configurable delay between each run. Sources are always captured;
    products and entities are opt-in.
    """
    flow_logger = get_run_logger()
    batch_ids = batch_ids or []
    if not batch_ids:
        raise ValueError("batch_ids must not be empty")

    flow_logger.info(
        "Starting batch extraction. batch_count=%s delay_seconds=%s capture_products=%s capture_entities=%s",
        len(batch_ids),
        delay_seconds,
        capture_products,
        capture_entities,
    )

    results = []
    for index, batch_id in enumerate(batch_ids, start=1):
        flow_logger.info("Running extraction %s/%s. batch_id=%s", index, len(batch_ids), batch_id)
        result = prompt_extraction_flow(
            batch_id=batch_id,
            limit=limit,
            skip=skip,
            dry_run=dry_run,
            force_rerun=force_rerun,
            llm_model_filter=llm_model_filter,
            auto_login=auto_login,
            login_email=login_email,
            capture_products=capture_products,
            capture_entities=capture_entities,
        )
        results.append({"batch_id": batch_id, "result": result})

        if index < len(batch_ids):
            next_batch_id = batch_ids[index]
            flow_logger.info(
                "Waiting %ss before next batch. next_batch_id=%s",
                delay_seconds,
                next_batch_id,
            )
            time.sleep(delay_seconds)

    flow_logger.info("Batch extraction finished. batch_count=%s", len(batch_ids))
    return {"batches": results, "total": len(batch_ids)}


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
