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
    extract_claude_batch_task,
    extract_google_ai_mode_batch_task,
    extract_google_ai_overview_batch_task,
    extract_perplexity_batch_task,
    product_output_process_task,
    prompt_output_process_task,
    score_workflow_trigger_task,
)


@flow(
    name="chatgpt-extraction-batch",
    flow_run_name="chatgpt-extraction-batch-{batch_id}",
    log_prints=True,
)
def prompt_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "gpt",
    limit: int = 5,
    skip: int = 0,
    auto_login: bool | None = False,
    login_email: str | None = None,
    capture_products: bool = True,
    capture_entities: bool = True,
    delay_seconds: int = 120,
    max_prompts: int | None = None,
    startup_delay_seconds: int = 0,
    trigger_scoring: bool = True,
) -> dict[str, Any]:
    """
    Sequentially run chatgpt-extraction until the currently remaining prompt set
    has been chunked into `limit`-sized runs, with a configurable delay between
    each run. Sources are always captured; products and entities are opt-in.

    max_prompts: when set by the dispatcher, caps the total number of prompts
    this worker processes and skips the mop-up pass.

    startup_delay_seconds: stagger delay set by the dispatcher (worker_index *
    stagger_seconds) so all workers don't hit external APIs simultaneously.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if startup_delay_seconds > 0:
        flow_logger.info("Staggered startup: waiting %ss before beginning work.", startup_delay_seconds)
        time.sleep(startup_delay_seconds)

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

    # Read required_models from batch config (drives multi-model completion check).
    llm_model_config = batch.get("llm_models") or {}
    required_models: list[str] | None = None
    if isinstance(llm_model_config, dict):
        raw = llm_model_config.get("required_models")
        if isinstance(raw, list) and raw:
            required_models = [str(m) for m in raw]
            flow_logger.info(
                "Batch config specifies required_models=%s — prompts missing any model will be re-run. batch_id=%s",
                required_models,
                batch_id,
            )

    remaining_prompts = api.get_prompts(
        batch_id,
        str(brand_id),
        only_remaining=True,
        llm_model_filter=model_filter,
        required_models=required_models,
    )
    remaining_count = max(0, len(remaining_prompts) - skip)
    run_count = math.ceil(remaining_count / limit) if remaining_count else 0
    if max_prompts is not None:
        run_count = min(run_count, math.ceil(max_prompts / limit))
    flow_logger.info(
        "Starting sequential prompt extraction batch. batch_id=%s brand_id=%s model_filter=%s remaining_count=%s skip=%s limit_per_run=%s planned_runs=%s auto_login=%s capture_products=%s capture_entities=%s delay_seconds=%s max_prompts=%s trigger_scoring=%s required_models=%s",
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
        max_prompts,
        trigger_scoring,
        required_models or "none",
    )

    run_results: list[dict[str, Any]] = []
    consecutive_all_failed = 0
    stopped_reason: str | None = None
    for run_index in range(1, run_count + 1):
        run_skip = skip
        effective_limit = min(limit, max_prompts - (run_index - 1) * limit) if max_prompts is not None else limit
        flow_logger.info(
            "Starting sequential chatgpt-extraction run %s/%s. batch_id=%s limit=%s skip=%s",
            run_index,
            run_count,
            batch_id,
            effective_limit,
            run_skip,
        )
        result = prompt_extraction_flow(
            batch_id=batch_id,
            limit=effective_limit,
            skip=run_skip,
            llm_model_filter=model_filter,
            auto_login=auto_login,
            login_email=login_email,
            force_rerun=False,
            capture_products=capture_products,
            capture_entities=capture_entities,
            trigger_scoring=trigger_scoring,
        )
        run_results.append(result)
        flow_logger.info(
            "Finished sequential chatgpt-extraction run %s/%s. saved_count=%s skipped_count=%s failed_count=%s",
            run_index,
            run_count,
            result.get("saved_count", 0),
            result.get("skipped_count", 0),
            result.get("failed_count", 0),
        )
        if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
            consecutive_all_failed += 1
            flow_logger.warning(
                "All prompts failed in run %s/%s (consecutive_all_failed=%s). batch_id=%s",
                run_index,
                run_count,
                consecutive_all_failed,
                batch_id,
            )
            if consecutive_all_failed >= 2:
                stopped_reason = "consecutive_all_failed"
                flow_logger.warning(
                    "Stopping batch after %s consecutive all-failed runs. batch_id=%s",
                    consecutive_all_failed,
                    batch_id,
                )
                break
        else:
            consecutive_all_failed = 0

        # Early-exit: if nothing was saved this run, check whether any prompts
        # are still outstanding. If other workers have claimed everything, there
        # is no point continuing the loop.
        if result.get("saved_count", 0) == 0 and run_index < run_count:
            still_remaining = api.get_prompts(
                batch_id,
                str(brand_id),
                only_remaining=True,
                llm_model_filter=model_filter,
                required_models=required_models,
            )
            if not still_remaining:
                stopped_reason = "batch_exhausted"
                flow_logger.info(
                    "Early exit: no prompts remaining after run %s/%s — batch already complete. batch_id=%s",
                    run_index,
                    run_count,
                    batch_id,
                )
                break
            flow_logger.info(
                "saved_count=0 on run %s/%s but %s prompt(s) still remaining — continuing. batch_id=%s",
                run_index,
                run_count,
                len(still_remaining),
                batch_id,
            )

        if run_index < run_count:
            flow_logger.info("Waiting %ss before next run.", delay_seconds)
            time.sleep(delay_seconds)

    saved_count = sum(int(r.get("saved_count") or 0) for r in run_results)
    failed_count = sum(int(r.get("failed_count") or 0) for r in run_results)
    skipped_count = sum(int(r.get("skipped_count") or 0) for r in run_results)

    # Mop-up pass — skipped when max_prompts is set (dispatcher ensures full coverage).
    mop_up_results: list[dict[str, Any]] = []
    mop_up_count = 0
    if stopped_reason is None and max_prompts is None:
        mop_up_remaining = api.get_prompts(
            batch_id,
            str(brand_id),
            only_remaining=True,
            llm_model_filter=model_filter,
            required_models=required_models,
        )
        mop_up_count = len(mop_up_remaining)
        flow_logger.info(
            "Batch-check: %s prompt(s) still remaining after initial run. batch_id=%s",
            mop_up_count,
            batch_id,
        )

        if mop_up_count > 0:
            mop_up_run_count = math.ceil(mop_up_count / limit)
            consecutive_all_failed = 0
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
                    trigger_scoring=trigger_scoring,
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
                if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
                    consecutive_all_failed += 1
                    if consecutive_all_failed >= 2:
                        stopped_reason = "consecutive_all_failed_mop_up"
                        flow_logger.warning(
                            "Stopping mop-up after %s consecutive all-failed runs. batch_id=%s",
                            consecutive_all_failed,
                            batch_id,
                        )
                        break
                else:
                    consecutive_all_failed = 0

                # Early-exit: nothing saved — check if batch is already complete.
                if result.get("saved_count", 0) == 0 and run_index < mop_up_run_count:
                    still_remaining = api.get_prompts(
                        batch_id,
                        str(brand_id),
                        only_remaining=True,
                        llm_model_filter=model_filter,
                        required_models=required_models,
                    )
                    if not still_remaining:
                        stopped_reason = "batch_exhausted"
                        flow_logger.info(
                            "Early exit: no prompts remaining after mop-up run %s/%s — batch already complete. batch_id=%s",
                            run_index,
                            mop_up_run_count,
                            batch_id,
                        )
                        break

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

    status = (
        "stopped_consecutive_failures"
        if stopped_reason
        else ("completed" if failed_count == 0 else "completed_with_failures")
    )
    summary = {
        "status": status,
        "batch_id": batch_id,
        "brand_id": str(brand_id),
        "model_filter": model_filter,
        "skip": skip,
        "auto_login": auto_login,
        "capture_products": capture_products,
        "capture_entities": capture_entities,
        "trigger_scoring": trigger_scoring,
        "delay_seconds": delay_seconds,
        "max_prompts": max_prompts,
        "initial_remaining_count": remaining_count,
        "limit_per_run": limit,
        "planned_runs": run_count,
        "completed_runs": len(run_results),
        "mop_up_remaining_count": mop_up_count,
        "mop_up_runs": len(mop_up_results),
        "saved_count": saved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "stopped_reason": stopped_reason,
        "runs": run_results,
        "mop_up_run_results": mop_up_results,
    }
    flow_logger.info("Sequential prompt extraction batch finished: %s", summary)
    return summary


@flow(
    name="chatgpt-extraction",
    flow_run_name="chatgpt-extraction-{batch_id}",
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
    capture_products: bool = True,
    capture_entities: bool = True,
    trigger_scoring: bool = True,
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
    if not dry_run and trigger_scoring and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )
    else:
        flow_logger.info(
            "Skipping score workflow trigger. trigger_scoring=%s saved_outputs=%s",
            trigger_scoring,
            len(result.get("saved_outputs") or []),
        )

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
    name="claude-extraction",
    flow_run_name="claude-extraction-{batch_id}",
    log_prints=True,
)
def claude_extraction_flow(
    batch_id: str | None = None,
    prompts_file: str | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    force_rerun: bool = False,
    llm_model_filter: str | None = "claude",
    measurements_filter: str | None = None,
    trigger_scoring: bool = True,
) -> dict[str, Any]:
    """Orchestrate a Claude.ai prompt extraction run."""
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    flow_logger.info(
        "Starting Claude extraction flow. batch_id=%s prompts_file=%s brand_id=%s limit=%s skip=%s force_rerun=%s llm_model_filter=%s measurements_filter=%s",
        batch_id,
        prompts_file,
        brand_id,
        limit,
        skip,
        force_rerun,
        llm_model_filter or "any",
        measurements_filter or "any",
    )
    result = extract_claude_batch_task(
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        measurements_filter=measurements_filter,
    )

    processing_result: dict[str, Any] | None = None
    if not dry_run and result.get("saved_count", 0) > 0:
        processing_result = prompt_output_process_task(
            saved_outputs=result.get("saved_outputs") or [],
            batch_id=result.get("batch_id") or batch_id,
            brand_id=result.get("brand_id") or brand_id,
            limit=result.get("saved_count") or limit or 50,
        )

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and trigger_scoring and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )

    combined_result = {
        **result,
        "prompt_output_processing": processing_result,
        "score_workflow_trigger": score_workflow_result,
    }
    flow_logger.info("Claude extraction flow finished: %s", combined_result)
    return combined_result


@flow(
    name="claude-extraction-batch",
    flow_run_name="claude-extraction-batch-{batch_id}",
    log_prints=True,
)
def claude_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "claude",
    limit: int = 5,
    skip: int = 0,
    delay_seconds: int = 120,
    max_prompts: int | None = None,
    startup_delay_seconds: int = 0,
    trigger_scoring: bool = True,
    measurements_filter: str | None = None,
) -> dict[str, Any]:
    """
    Sequentially run claude-extraction until the remaining prompt set is exhausted.
    Mirrors prompt_extraction_batch_flow for ChatGPT.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if startup_delay_seconds > 0:
        flow_logger.info("Staggered startup: waiting %ss before beginning work.", startup_delay_seconds)
        time.sleep(startup_delay_seconds)

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
        measurements_filter=measurements_filter,
    )
    remaining_count = max(0, len(remaining_prompts) - skip)
    run_count = math.ceil(remaining_count / limit) if remaining_count else 0
    if max_prompts is not None:
        run_count = min(run_count, math.ceil(max_prompts / limit))
    flow_logger.info(
        "Claude batch flow: batch_id=%s brand_id=%s remaining=%s run_count=%s limit=%s measurements_filter=%s",
        batch_id,
        brand_id,
        remaining_count,
        run_count,
        limit,
        measurements_filter or "any",
    )

    if run_count == 0:
        flow_logger.info("No remaining Claude prompts. Exiting.")
        return {"status": "no_prompts", "batch_id": batch_id, "runs_completed": 0}

    total_saved = 0
    all_saved_outputs: list[dict[str, Any]] = []
    for run_index in range(run_count):
        flow_logger.info("Claude batch run %s/%s. batch_id=%s", run_index + 1, run_count, batch_id)
        run_result = claude_extraction_flow(
            batch_id=batch_id,
            limit=limit,
            skip=skip,
            llm_model_filter=model_filter,
            measurements_filter=measurements_filter,
            trigger_scoring=trigger_scoring,
        )
        saved = run_result.get("saved_count") or 0
        total_saved += saved
        all_saved_outputs.extend(run_result.get("saved_outputs") or [])
        flow_logger.info(
            "Claude batch run %s/%s finished. saved=%s total_saved=%s",
            run_index + 1,
            run_count,
            saved,
            total_saved,
        )
        if run_index < run_count - 1 and delay_seconds > 0:
            flow_logger.info("Waiting %ss before next Claude run.", delay_seconds)
            time.sleep(delay_seconds)

    return {
        "status": "completed",
        "batch_id": batch_id,
        "runs_completed": run_count,
        "total_saved": total_saved,
    }


@flow(
    name="perplexity-extraction",
    flow_run_name="perplexity-extraction-{batch_id}",
    log_prints=True,
)
def perplexity_extraction_flow(
    batch_id: str | None = None,
    prompts_file: str | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    force_rerun: bool = False,
    llm_model_filter: str | None = "perplexity",
    measurements_filter: str | None = None,
    trigger_scoring: bool = True,
) -> dict[str, Any]:
    """Orchestrate a Perplexity.ai prompt extraction run."""
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    flow_logger.info(
        "Starting Perplexity extraction flow. batch_id=%s limit=%s measurements_filter=%s",
        batch_id,
        limit,
        measurements_filter or "any",
    )
    result = extract_perplexity_batch_task(
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        measurements_filter=measurements_filter,
    )

    processing_result: dict[str, Any] | None = None
    if not dry_run and result.get("saved_count", 0) > 0:
        processing_result = prompt_output_process_task(
            saved_outputs=result.get("saved_outputs") or [],
            batch_id=result.get("batch_id") or batch_id,
            brand_id=result.get("brand_id") or brand_id,
            limit=result.get("saved_count") or limit or 50,
        )

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and trigger_scoring and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )

    combined_result = {
        **result,
        "prompt_output_processing": processing_result,
        "score_workflow_trigger": score_workflow_result,
    }
    flow_logger.info("Perplexity extraction flow finished: %s", combined_result)
    return combined_result


@flow(
    name="perplexity-extraction-batch",
    flow_run_name="perplexity-extraction-batch-{batch_id}",
    log_prints=True,
)
def perplexity_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "perplexity",
    limit: int = 5,
    skip: int = 0,
    delay_seconds: int = 120,
    max_prompts: int | None = None,
    startup_delay_seconds: int = 0,
    trigger_scoring: bool = True,
    measurements_filter: str | None = None,
) -> dict[str, Any]:
    """
    Sequentially run perplexity-extraction until the remaining prompt set is exhausted.
    Mirrors claude_extraction_batch_flow.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if startup_delay_seconds > 0:
        flow_logger.info("Staggered startup: waiting %ss before beginning work.", startup_delay_seconds)
        time.sleep(startup_delay_seconds)

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
        measurements_filter=measurements_filter,
    )
    remaining_count = max(0, len(remaining_prompts) - skip)
    run_count = math.ceil(remaining_count / limit) if remaining_count else 0
    if max_prompts is not None:
        run_count = min(run_count, math.ceil(max_prompts / limit))
    flow_logger.info(
        "Perplexity batch flow: batch_id=%s brand_id=%s remaining=%s run_count=%s limit=%s measurements_filter=%s",
        batch_id,
        brand_id,
        remaining_count,
        run_count,
        limit,
        measurements_filter or "any",
    )

    if run_count == 0:
        flow_logger.info("No remaining Perplexity prompts. Exiting.")
        return {"status": "no_prompts", "batch_id": batch_id, "runs_completed": 0}

    total_saved = 0
    all_saved_outputs: list[dict[str, Any]] = []
    for run_index in range(run_count):
        flow_logger.info("Perplexity batch run %s/%s. batch_id=%s", run_index + 1, run_count, batch_id)
        run_result = perplexity_extraction_flow(
            batch_id=batch_id,
            limit=limit,
            skip=skip,
            llm_model_filter=model_filter,
            measurements_filter=measurements_filter,
            trigger_scoring=trigger_scoring,
        )
        saved = run_result.get("saved_count") or 0
        total_saved += saved
        all_saved_outputs.extend(run_result.get("saved_outputs") or [])
        flow_logger.info(
            "Perplexity batch run %s/%s finished. saved=%s total_saved=%s",
            run_index + 1,
            run_count,
            saved,
            total_saved,
        )
        if run_index < run_count - 1 and delay_seconds > 0:
            flow_logger.info("Waiting %ss before next Perplexity run.", delay_seconds)
            time.sleep(delay_seconds)

    return {
        "status": "completed",
        "batch_id": batch_id,
        "runs_completed": run_count,
        "total_saved": total_saved,
    }


@flow(
    name="google-ai-mode-extraction-batch",
    flow_run_name="google-ai-mode-extraction-batch-{batch_id}",
    log_prints=True,
)
def google_ai_mode_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "google-ai-mode",
    limit: int = 5,
    skip: int = 0,
    delay_seconds: int = 60,
    country: str | None = None,
    language: str | None = None,
    use_proxy: bool = False,
    max_prompts: int | None = None,
    startup_delay_seconds: int = 0,
    trigger_scoring: bool = True,
) -> dict[str, Any]:
    """
    Sequentially run google-ai-mode-extraction until all remaining prompts in
    the batch are processed, chunked into `limit`-sized runs with a configurable
    delay between each run.

    max_prompts: when set by the dispatcher, caps the total number of prompts
    this worker processes and skips the mop-up pass.

    startup_delay_seconds: stagger delay set by the dispatcher (worker_index *
    stagger_seconds) so workers don't all launch Chrome and hit Google at once.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if startup_delay_seconds > 0:
        flow_logger.info("Staggered startup: waiting %ss before beginning work.", startup_delay_seconds)
        time.sleep(startup_delay_seconds)

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
    if max_prompts is not None:
        run_count = min(run_count, math.ceil(max_prompts / limit))
    flow_logger.info(
        "Starting sequential Google AI Mode batch. batch_id=%s brand_id=%s model_filter=%s remaining_count=%s skip=%s limit_per_run=%s planned_runs=%s delay_seconds=%s country=%s language=%s max_prompts=%s",
        batch_id,
        brand_id,
        model_filter or "any",
        remaining_count,
        skip,
        limit,
        run_count,
        delay_seconds,
        country or "<env>",
        language or "<env>",
        max_prompts,
    )

    run_results: list[dict[str, Any]] = []
    consecutive_all_failed = 0
    stopped_reason: str | None = None
    for run_index in range(1, run_count + 1):
        run_skip = skip
        effective_limit = min(limit, max_prompts - (run_index - 1) * limit) if max_prompts is not None else limit
        flow_logger.info(
            "Starting Google AI Mode run %s/%s. batch_id=%s limit=%s skip=%s",
            run_index,
            run_count,
            batch_id,
            effective_limit,
            run_skip,
        )
        result = google_ai_mode_extraction_flow(
            batch_id=batch_id,
            limit=effective_limit,
            skip=run_skip,
            llm_model_filter=model_filter,
            force_rerun=False,
            country=country,
            language=language,
            use_proxy=use_proxy,
            trigger_scoring=trigger_scoring,
        )
        run_results.append(result)
        flow_logger.info(
            "Finished Google AI Mode run %s/%s. saved_count=%s skipped_count=%s failed_count=%s",
            run_index,
            run_count,
            result.get("saved_count", 0),
            result.get("skipped_count", 0),
            result.get("failed_count", 0),
        )
        if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
            consecutive_all_failed += 1
            flow_logger.warning(
                "All prompts failed in run %s/%s (consecutive_all_failed=%s). batch_id=%s",
                run_index,
                run_count,
                consecutive_all_failed,
                batch_id,
            )
            if consecutive_all_failed >= 2:
                stopped_reason = "google_blocked_consecutive"
                flow_logger.warning(
                    "Stopping batch after %s consecutive all-failed runs — Google is blocking requests. batch_id=%s",
                    consecutive_all_failed,
                    batch_id,
                )
                break
        else:
            consecutive_all_failed = 0

        # Early-exit: if nothing was saved this run, check whether any prompts
        # are still outstanding. If other workers have claimed everything, there
        # is no point continuing the loop.
        if result.get("saved_count", 0) == 0 and run_index < run_count:
            still_remaining = api.get_prompts(
                batch_id,
                str(brand_id),
                only_remaining=True,
                llm_model_filter=model_filter,
            )
            if not still_remaining:
                stopped_reason = "batch_exhausted"
                flow_logger.info(
                    "Early exit: no prompts remaining after run %s/%s — batch already complete. batch_id=%s",
                    run_index,
                    run_count,
                    batch_id,
                )
                break
            flow_logger.info(
                "saved_count=0 on run %s/%s but %s prompt(s) still remaining — continuing. batch_id=%s",
                run_index,
                run_count,
                len(still_remaining),
                batch_id,
            )

        if run_index < run_count:
            flow_logger.info("Waiting %ss before next run.", delay_seconds)
            time.sleep(delay_seconds)

    saved_count = sum(int(r.get("saved_count") or 0) for r in run_results)
    failed_count = sum(int(r.get("failed_count") or 0) for r in run_results)
    skipped_count = sum(int(r.get("skipped_count") or 0) for r in run_results)

    # Mop-up pass — skipped when max_prompts is set (dispatcher ensures full coverage).
    mop_up_results: list[dict[str, Any]] = []
    mop_up_count = 0
    if stopped_reason is None and max_prompts is None:
        mop_up_remaining = api.get_prompts(batch_id, str(brand_id), only_remaining=True, llm_model_filter=model_filter)
        mop_up_count = len(mop_up_remaining)
        flow_logger.info(
            "Batch-check: %s prompt(s) still remaining after initial run. batch_id=%s", mop_up_count, batch_id
        )

        if mop_up_count > 0:
            mop_up_run_count = math.ceil(mop_up_count / limit)
            consecutive_all_failed = 0
            flow_logger.info(
                "Starting mop-up pass: %s run(s) of limit=%s. batch_id=%s", mop_up_run_count, limit, batch_id
            )
            for run_index in range(1, mop_up_run_count + 1):
                result = google_ai_mode_extraction_flow(
                    batch_id=batch_id,
                    limit=limit,
                    skip=0,
                    llm_model_filter=model_filter,
                    force_rerun=False,
                    country=country,
                    language=language,
                    use_proxy=use_proxy,
                    trigger_scoring=trigger_scoring,
                )
                mop_up_results.append(result)
                flow_logger.info(
                    "Mop-up run %s/%s finished. saved_count=%s failed_count=%s",
                    run_index,
                    mop_up_run_count,
                    result.get("saved_count", 0),
                    result.get("failed_count", 0),
                )
                if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
                    consecutive_all_failed += 1
                    if consecutive_all_failed >= 2:
                        stopped_reason = "google_blocked_consecutive_mop_up"
                        flow_logger.warning(
                            "Stopping mop-up after %s consecutive all-failed runs — Google is blocking requests. batch_id=%s",
                            consecutive_all_failed,
                            batch_id,
                        )
                        break
                else:
                    consecutive_all_failed = 0

                # Early-exit: nothing saved — check if batch is already complete.
                if result.get("saved_count", 0) == 0 and run_index < mop_up_run_count:
                    still_remaining = api.get_prompts(
                        batch_id,
                        str(brand_id),
                        only_remaining=True,
                        llm_model_filter=model_filter,
                    )
                    if not still_remaining:
                        stopped_reason = "batch_exhausted"
                        flow_logger.info(
                            "Early exit: no prompts remaining after mop-up run %s/%s — batch already complete. batch_id=%s",
                            run_index,
                            mop_up_run_count,
                            batch_id,
                        )
                        break

                if run_index < mop_up_run_count:
                    time.sleep(delay_seconds)

            mop_up_saved = sum(int(r.get("saved_count") or 0) for r in mop_up_results)
            mop_up_failed = sum(int(r.get("failed_count") or 0) for r in mop_up_results)
            saved_count += mop_up_saved
            failed_count += mop_up_failed
            flow_logger.info("Mop-up pass complete. mop_up_saved=%s mop_up_failed=%s", mop_up_saved, mop_up_failed)

    status = (
        "stopped_google_blocked"
        if stopped_reason
        else ("completed" if failed_count == 0 else "completed_with_failures")
    )
    summary = {
        "status": status,
        "batch_id": batch_id,
        "brand_id": str(brand_id),
        "model_filter": model_filter,
        "skip": skip,
        "delay_seconds": delay_seconds,
        "country": country,
        "language": language,
        "use_proxy": use_proxy,
        "trigger_scoring": trigger_scoring,
        "max_prompts": max_prompts,
        "initial_remaining_count": remaining_count,
        "limit_per_run": limit,
        "planned_runs": run_count,
        "completed_runs": len(run_results),
        "mop_up_remaining_count": mop_up_count,
        "mop_up_runs": len(mop_up_results),
        "saved_count": saved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "stopped_reason": stopped_reason,
        "runs": run_results,
        "mop_up_run_results": mop_up_results,
    }
    flow_logger.info("Google AI Mode batch finished: %s", summary)
    return summary


@flow(
    name="google-ai-overview-extraction-batch",
    flow_run_name="google-ai-overview-extraction-batch-{batch_id}",
    log_prints=True,
)
def google_ai_overview_extraction_batch_flow(
    batch_id: str | None = None,
    model_filter: str | None = "google-ai-overview",
    limit: int = 5,
    skip: int = 0,
    delay_seconds: int = 60,
    country: str | None = None,
    language: str | None = None,
    use_proxy: bool = False,
    max_prompts: int | None = None,
    startup_delay_seconds: int = 0,
    trigger_scoring: bool = True,
    paa_titles_only: bool = True,
) -> dict[str, Any]:
    """
    Sequentially run google-ai-overview-extraction until all remaining prompts
    in the batch are processed, chunked into `limit`-sized runs with a
    configurable delay between each run.

    max_prompts: when set by the dispatcher, caps the total number of prompts
    this worker processes and skips the mop-up pass (the dispatcher ensures
    full coverage across workers without overlap).

    startup_delay_seconds: stagger delay set by the dispatcher (worker_index *
    stagger_seconds) so workers don't all launch Chrome and hit Google at once.
    """
    flow_logger = get_run_logger()
    flow_logger.info("WORKER machine_id=%s", os.getenv("FLY_MACHINE_ID", "local"))
    if not batch_id:
        raise ValueError("batch_id is required")
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if startup_delay_seconds > 0:
        flow_logger.info("Staggered startup: waiting %ss before beginning work.", startup_delay_seconds)
        time.sleep(startup_delay_seconds)

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
    if max_prompts is not None:
        run_count = min(run_count, math.ceil(max_prompts / limit))
    flow_logger.info(
        "Starting sequential Google AI Overview batch. batch_id=%s brand_id=%s model_filter=%s remaining_count=%s skip=%s limit_per_run=%s planned_runs=%s delay_seconds=%s country=%s language=%s use_proxy=%s max_prompts=%s",
        batch_id,
        brand_id,
        model_filter or "any",
        remaining_count,
        skip,
        limit,
        run_count,
        delay_seconds,
        country or "<env>",
        language or "<env>",
        use_proxy,
        max_prompts,
    )

    run_results: list[dict[str, Any]] = []
    consecutive_all_failed = 0
    stopped_reason: str | None = None
    for run_index in range(1, run_count + 1):
        run_skip = skip
        effective_limit = min(limit, max_prompts - (run_index - 1) * limit) if max_prompts is not None else limit
        flow_logger.info(
            "Starting Google AI Overview run %s/%s. batch_id=%s limit=%s skip=%s",
            run_index,
            run_count,
            batch_id,
            effective_limit,
            run_skip,
        )
        result = google_ai_overview_extraction_flow(
            batch_id=batch_id,
            limit=effective_limit,
            skip=run_skip,
            llm_model_filter=model_filter,
            force_rerun=False,
            country=country,
            language=language,
            use_proxy=use_proxy,
            trigger_scoring=trigger_scoring,
            paa_titles_only=paa_titles_only,
        )
        run_results.append(result)
        flow_logger.info(
            "Finished Google AI Overview run %s/%s. saved_count=%s skipped_count=%s failed_count=%s",
            run_index,
            run_count,
            result.get("saved_count", 0),
            result.get("skipped_count", 0),
            result.get("failed_count", 0),
        )
        if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
            consecutive_all_failed += 1
            flow_logger.warning(
                "All prompts failed in run %s/%s (consecutive_all_failed=%s). batch_id=%s",
                run_index,
                run_count,
                consecutive_all_failed,
                batch_id,
            )
            if consecutive_all_failed >= 2:
                stopped_reason = "google_blocked_consecutive"
                flow_logger.warning(
                    "Stopping batch after %s consecutive all-failed runs — Google is blocking requests. batch_id=%s",
                    consecutive_all_failed,
                    batch_id,
                )
                break
        else:
            consecutive_all_failed = 0

        # Early-exit: if nothing was saved this run, check whether any prompts
        # are still outstanding. If other workers have claimed everything, there
        # is no point continuing the loop.
        if result.get("saved_count", 0) == 0 and run_index < run_count:
            still_remaining = api.get_prompts(
                batch_id,
                str(brand_id),
                only_remaining=True,
                llm_model_filter=model_filter,
            )
            if not still_remaining:
                stopped_reason = "batch_exhausted"
                flow_logger.info(
                    "Early exit: no prompts remaining after run %s/%s — batch already complete. batch_id=%s",
                    run_index,
                    run_count,
                    batch_id,
                )
                break
            flow_logger.info(
                "saved_count=0 on run %s/%s but %s prompt(s) still remaining — continuing. batch_id=%s",
                run_index,
                run_count,
                len(still_remaining),
                batch_id,
            )

        if run_index < run_count:
            flow_logger.info("Waiting %ss before next run.", delay_seconds)
            time.sleep(delay_seconds)

    saved_count = sum(int(r.get("saved_count") or 0) for r in run_results)
    failed_count = sum(int(r.get("failed_count") or 0) for r in run_results)
    skipped_count = sum(int(r.get("skipped_count") or 0) for r in run_results)

    # Mop-up pass — skipped when max_prompts is set (dispatcher ensures full
    # coverage; mop-up would risk overlapping with other workers' ranges).
    mop_up_results: list[dict[str, Any]] = []
    mop_up_count = 0
    if stopped_reason is None and max_prompts is None:
        mop_up_remaining = api.get_prompts(batch_id, str(brand_id), only_remaining=True, llm_model_filter=model_filter)
        mop_up_count = len(mop_up_remaining)
        flow_logger.info(
            "Batch-check: %s prompt(s) still remaining after initial run. batch_id=%s", mop_up_count, batch_id
        )

        if mop_up_count > 0:
            mop_up_run_count = math.ceil(mop_up_count / limit)
            consecutive_all_failed = 0
            flow_logger.info(
                "Starting mop-up pass: %s run(s) of limit=%s. batch_id=%s", mop_up_run_count, limit, batch_id
            )
            for run_index in range(1, mop_up_run_count + 1):
                result = google_ai_overview_extraction_flow(
                    batch_id=batch_id,
                    limit=limit,
                    skip=0,
                    llm_model_filter=model_filter,
                    force_rerun=False,
                    country=country,
                    language=language,
                    use_proxy=use_proxy,
                    trigger_scoring=trigger_scoring,
                )
                mop_up_results.append(result)
                flow_logger.info(
                    "Mop-up run %s/%s finished. saved_count=%s failed_count=%s",
                    run_index,
                    mop_up_run_count,
                    result.get("saved_count", 0),
                    result.get("failed_count", 0),
                )
                if result.get("saved_count", 0) == 0 and result.get("failed_count", 0) > 0:
                    consecutive_all_failed += 1
                    if consecutive_all_failed >= 2:
                        stopped_reason = "google_blocked_consecutive_mop_up"
                        flow_logger.warning(
                            "Stopping mop-up after %s consecutive all-failed runs — Google is blocking requests. batch_id=%s",
                            consecutive_all_failed,
                            batch_id,
                        )
                        break
                else:
                    consecutive_all_failed = 0

                # Early-exit: nothing saved — check if batch is already complete.
                if result.get("saved_count", 0) == 0 and run_index < mop_up_run_count:
                    still_remaining = api.get_prompts(
                        batch_id,
                        str(brand_id),
                        only_remaining=True,
                        llm_model_filter=model_filter,
                    )
                    if not still_remaining:
                        stopped_reason = "batch_exhausted"
                        flow_logger.info(
                            "Early exit: no prompts remaining after mop-up run %s/%s — batch already complete. batch_id=%s",
                            run_index,
                            mop_up_run_count,
                            batch_id,
                        )
                        break

                if run_index < mop_up_run_count:
                    time.sleep(delay_seconds)

            mop_up_saved = sum(int(r.get("saved_count") or 0) for r in mop_up_results)
            mop_up_failed = sum(int(r.get("failed_count") or 0) for r in mop_up_results)
            saved_count += mop_up_saved
            failed_count += mop_up_failed
            flow_logger.info("Mop-up pass complete. mop_up_saved=%s mop_up_failed=%s", mop_up_saved, mop_up_failed)

    status = (
        "stopped_google_blocked"
        if stopped_reason
        else ("completed" if failed_count == 0 else "completed_with_failures")
    )
    summary = {
        "status": status,
        "batch_id": batch_id,
        "brand_id": str(brand_id),
        "model_filter": model_filter,
        "skip": skip,
        "delay_seconds": delay_seconds,
        "country": country,
        "language": language,
        "use_proxy": use_proxy,
        "trigger_scoring": trigger_scoring,
        "max_prompts": max_prompts,
        "initial_remaining_count": remaining_count,
        "limit_per_run": limit,
        "planned_runs": run_count,
        "completed_runs": len(run_results),
        "mop_up_remaining_count": mop_up_count,
        "mop_up_runs": len(mop_up_results),
        "saved_count": saved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "stopped_reason": stopped_reason,
        "runs": run_results,
        "mop_up_run_results": mop_up_results,
    }
    flow_logger.info("Google AI Overview batch finished: %s", summary)
    return summary


@flow(
    name="google-ai-mode-extraction",
    flow_run_name="google-ai-mode-extraction-{batch_id}",
    log_prints=True,
)
def google_ai_mode_extraction_flow(
    batch_id: str | None = None,
    prompts_file: str | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    force_rerun: bool = False,
    llm_model_filter: str | None = "google-ai-mode",
    country: str | None = None,
    language: str | None = None,
    debug_pause_seconds: int = 0,
    use_proxy: bool = False,
    trigger_scoring: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate a Google AI Mode prompt extraction run.
    """
    flow_logger = get_run_logger()
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    flow_logger.info(
        "Starting Google AI Mode extraction flow. batch_id=%s prompts_file=%s brand_id=%s limit=%s skip=%s force_rerun=%s llm_model_filter=%s country=%s language=%s",
        batch_id,
        prompts_file,
        brand_id,
        limit,
        skip,
        force_rerun,
        llm_model_filter or "any",
        country or "<env>",
        language or "<env>",
    )
    result = extract_google_ai_mode_batch_task(
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        country=country,
        language=language,
        debug_pause_seconds=debug_pause_seconds,
        use_proxy=use_proxy,
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
        flow_logger.info("Skipping prompt output processing because no Google AI Mode outputs were saved.")

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and trigger_scoring and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )
    else:
        flow_logger.info(
            "Skipping score workflow trigger. trigger_scoring=%s saved_outputs=%s",
            trigger_scoring,
            len(result.get("saved_outputs") or []),
        )

    combined_result = {
        **result,
        "prompt_output_processing": processing_result,
        "score_workflow_trigger": score_workflow_result,
    }
    flow_logger.info("Google AI Mode extraction flow finished: %s", combined_result)
    return combined_result


@flow(
    name="google-ai-overview-extraction",
    flow_run_name="google-ai-overview-extraction-{batch_id}",
    log_prints=True,
)
def google_ai_overview_extraction_flow(
    batch_id: str | None = None,
    prompts_file: str | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    force_rerun: bool = False,
    llm_model_filter: str | None = "google-ai-overview",
    country: str | None = None,
    language: str | None = None,
    debug_pause_seconds: int = 0,
    use_proxy: bool = False,
    trigger_scoring: bool = True,
    paa_titles_only: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate a Google AI Overview prompt extraction run.
    """
    flow_logger = get_run_logger()
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    flow_logger.info(
        "Starting Google AI Overview extraction flow. batch_id=%s prompts_file=%s brand_id=%s limit=%s skip=%s force_rerun=%s llm_model_filter=%s country=%s language=%s",
        batch_id,
        prompts_file,
        brand_id,
        limit,
        skip,
        force_rerun,
        llm_model_filter or "any",
        country or "<env>",
        language or "<env>",
    )
    result = extract_google_ai_overview_batch_task(
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        limit=limit,
        skip=skip,
        dry_run=dry_run,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        country=country,
        language=language,
        debug_pause_seconds=debug_pause_seconds,
        use_proxy=use_proxy,
        paa_titles_only=paa_titles_only,
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
        flow_logger.info("Skipping prompt output processing because no Google AI Overview outputs were saved.")

    score_workflow_result: dict[str, Any] | None = None
    if not dry_run and trigger_scoring and result.get("saved_outputs"):
        score_workflow_result = score_workflow_trigger_task(
            saved_outputs=result.get("saved_outputs") or [], force=False
        )
    else:
        flow_logger.info(
            "Skipping score workflow trigger. trigger_scoring=%s saved_outputs=%s",
            trigger_scoring,
            len(result.get("saved_outputs") or []),
        )

    combined_result = {
        **result,
        "prompt_output_processing": processing_result,
        "score_workflow_trigger": score_workflow_result,
    }
    flow_logger.info("Google AI Overview extraction flow finished: %s", combined_result)
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
