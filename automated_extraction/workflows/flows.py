from __future__ import annotations

import math
import os
import time
from typing import Any

from prefect import flow
from prefect.logging import get_run_logger

from automated_extraction.api_client import ApiClient
from automated_extraction.config import Settings
from automated_extraction.workflows.tasks import (
    entity_output_process_task,
    extract_chatgpt_batch_task,
    product_output_process_task,
    prompt_output_process_task,
    score_workflow_trigger_task,
)


@flow(
    name="prompt-extraction-batch",
    flow_run_name="prompt-extraction-batch-{batch_id}",
    log_prints=True,
)
def prompt_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "gpt",
    limit: int = 10,
    skip: int = 0,
    auto_login: bool | None = False,
    login_email: str | None = None,
    capture_products: bool = False,
    capture_entities: bool = False,
    delay_seconds: int = 120,
) -> dict[str, Any]:
    """
    Sequentially run prompt-extraction until the currently remaining prompt set
    has been chunked into `limit`-sized runs, with a configurable delay between
    each run. Sources are always captured; products and entities are opt-in.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")

    settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)
    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )

    batch = api.get_batch(batch_id)
    brand_id = batch.get("brand_id")
    if not brand_id:
        raise RuntimeError(f"Batch {batch_id} does not include brand_id")

    remaining_prompts = api.get_prompts(
        batch_id,
        str(brand_id),
        only_remaining=True,
        llm_model_filter=model_filter,
    )
    remaining_count = max(0, len(remaining_prompts) - skip)
    run_count = math.ceil(remaining_count / limit) if remaining_count else 0
    flow_logger.info(
        "Starting sequential prompt extraction batch. batch_id=%s brand_id=%s model_filter=%s remaining_count=%s skip=%s limit_per_run=%s planned_runs=%s auto_login=%s capture_products=%s capture_entities=%s delay_seconds=%s",
        batch_id,
        brand_id,
        model_filter or "any",
        remaining_count,
        skip,
        limit,
        run_count,
        auto_login,
        capture_products,
        capture_entities,
        delay_seconds,
    )

    run_results: list[dict[str, Any]] = []
    for run_index in range(1, run_count + 1):
        run_skip = skip if run_index == 1 else 0
        flow_logger.info(
            "Starting sequential prompt-extraction run %s/%s. batch_id=%s limit=%s skip=%s",
            run_index,
            run_count,
            batch_id,
            limit,
            run_skip,
        )
        result = prompt_extraction_flow(
            batch_id=batch_id,
            limit=limit,
            skip=run_skip,
            llm_model_filter=model_filter,
            auto_login=auto_login,
            login_email=login_email,
            force_rerun=False,
            capture_products=capture_products,
            capture_entities=capture_entities,
        )
        run_results.append(result)
        flow_logger.info(
            "Finished sequential prompt-extraction run %s/%s. saved_count=%s skipped_count=%s failed_count=%s",
            run_index,
            run_count,
            result.get("saved_count", 0),
            result.get("skipped_count", 0),
            result.get("failed_count", 0),
        )

        if run_index < run_count:
            flow_logger.info("Waiting %ss before next run.", delay_seconds)
            time.sleep(delay_seconds)

    saved_count = sum(int(r.get("saved_count") or 0) for r in run_results)
    failed_count = sum(int(r.get("failed_count") or 0) for r in run_results)
    skipped_count = sum(int(r.get("skipped_count") or 0) for r in run_results)

    # --- Batch-check pass ---
    # After all sequential runs complete, re-fetch remaining prompts once.
    # If any are still outstanding (missed due to race conditions or failures),
    # run one additional round of sequential batches to mop them up.
    mop_up_results: list[dict[str, Any]] = []
    mop_up_remaining = api.get_prompts(
        batch_id,
        str(brand_id),
        only_remaining=True,
        llm_model_filter=model_filter,
    )
    mop_up_count = len(mop_up_remaining)
    flow_logger.info(
        "Batch-check: %s prompt(s) still remaining after initial run. batch_id=%s",
        mop_up_count,
        batch_id,
    )

    if mop_up_count > 0:
        mop_up_run_count = math.ceil(mop_up_count / limit)
        flow_logger.info(
            "Starting mop-up pass: %s run(s) of limit=%s. batch_id=%s",
            mop_up_run_count,
            limit,
            batch_id,
        )
        for run_index in range(1, mop_up_run_count + 1):
            flow_logger.info(
                "Mop-up run %s/%s. batch_id=%s limit=%s",
                run_index,
                mop_up_run_count,
                batch_id,
                limit,
            )
            result = prompt_extraction_flow(
                batch_id=batch_id,
                limit=limit,
                skip=0,
                llm_model_filter=model_filter,
                auto_login=auto_login,
                login_email=login_email,
                force_rerun=False,
                capture_products=capture_products,
                capture_entities=capture_entities,
            )
            mop_up_results.append(result)
            flow_logger.info(
                "Mop-up run %s/%s finished. saved_count=%s skipped_count=%s failed_count=%s",
                run_index,
                mop_up_run_count,
                result.get("saved_count", 0),
                result.get("skipped_count", 0),
                result.get("failed_count", 0),
            )
            if run_index < mop_up_run_count:
                flow_logger.info("Waiting %ss before next mop-up run.", delay_seconds)
                time.sleep(delay_seconds)

        mop_up_saved = sum(int(r.get("saved_count") or 0) for r in mop_up_results)
        mop_up_failed = sum(int(r.get("failed_count") or 0) for r in mop_up_results)
        saved_count += mop_up_saved
        failed_count += mop_up_failed
        flow_logger.info(
            "Mop-up pass complete. mop_up_saved=%s mop_up_failed=%s",
            mop_up_saved,
            mop_up_failed,
        )

    status = "completed" if failed_count == 0 else "completed_with_failures"
    summary = {
        "status": status,
        "batch_id": batch_id,
        "brand_id": str(brand_id),
        "model_filter": model_filter,
        "skip": skip,
        "auto_login": auto_login,
        "capture_products": capture_products,
        "capture_entities": capture_entities,
        "delay_seconds": delay_seconds,
        "initial_remaining_count": remaining_count,
        "limit_per_run": limit,
        "planned_runs": run_count,
        "completed_runs": len(run_results),
        "mop_up_remaining_count": mop_up_count,
        "mop_up_runs": len(mop_up_results),
        "saved_count": saved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "runs": run_results,
        "mop_up_run_results": mop_up_results,
    }
    flow_logger.info("Sequential prompt extraction batch finished: %s", summary)
    return summary


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
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
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
        flow_logger.info(
            "Skipping product output processing. capture_products=%s refs=%s",
            capture_products,
            len(product_output_refs),
        )

    entity_processing_result: dict[str, Any] | None = None
    if not dry_run and capture_entities and entity_output_refs:
        entity_processing_result = entity_output_process_task(entity_output_refs=entity_output_refs)
    else:
        flow_logger.info(
            "Skipping entity output processing. capture_entities=%s refs=%s", capture_entities, len(entity_output_refs)
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

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )
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
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
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
