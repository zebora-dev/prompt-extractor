from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from prefect import task
from prefect.logging import get_run_logger

from automated_extraction.config import Settings
from automated_extraction.entity_output_processor import process_entity_outputs
from automated_extraction.extraction import run_extraction_job
from automated_extraction.product_output_processor import process_product_outputs
from automated_extraction.prompt_output_processor import process_prompt_outputs
from automated_extraction.workflow_trigger import trigger_score_workflows

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
    force_rerun: bool = False,
    llm_model_filter: str | None = "gpt",
    auto_login: bool | None = None,
    login_email: str | None = None,
    capture_products: bool = False,
    capture_entities: bool = False,
) -> dict[str, Any]:
    """
    Run the current CLI extraction process as one observable Prefect task.

    Keeping this as one task preserves the single logged-in browser lifecycle
    while still giving Prefect run state, logs, parameters, and retries.
    """
    task_logger = get_run_logger()
    task_logger.info(
        "Starting ChatGPT extraction task. batch_id=%s prompts_file=%s limit=%s skip=%s dry_run=%s force_rerun=%s llm_model_filter=%s auto_login=%s login_email=%s capture_products=%s capture_entities=%s",
        batch_id,
        prompts_file,
        limit,
        skip,
        dry_run,
        force_rerun,
        llm_model_filter or "any",
        auto_login,
        login_email or "<env>",
        capture_products,
        capture_entities,
    )
    settings = Settings.from_env(
        require_api_key=True,
        require_auto_login_credentials=(auto_login is True) or (auto_login is None),
    )
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
        force_rerun=force_rerun,
        llm_model_filter=llm_model_filter,
        auto_login=auto_login,
        login_email=login_email,
        capture_products=capture_products,
        capture_entities=capture_entities,
    )
    payload = asdict(result)
    task_logger.info("Finished ChatGPT extraction task: %s", summarize_extraction_payload(payload))
    if result.failed_count:
        task_logger.warning("Extraction completed with %s failed prompt(s).", result.failed_count)
    return payload


def summarize_extraction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload)
    product_outputs = summary.pop("product_outputs", []) or []
    entity_outputs = summary.pop("entity_outputs", []) or []
    summary["product_outputs_summary"] = {
        "output_ref_count": len(product_outputs),
        "product_count": sum(len(ref.get("products") or []) for ref in product_outputs if isinstance(ref, dict)),
    }
    summary["entity_outputs_summary"] = {
        "output_ref_count": len(entity_outputs),
        "entity_count": sum(len(ref.get("entities") or []) for ref in entity_outputs if isinstance(ref, dict)),
    }
    return summary


def _process_products_task_run_name() -> str:
    try:
        from prefect.runtime import task_run

        params = getattr(task_run, "parameters", None) or {}
        refs = params.get("product_output_refs") or []
        first_ref = refs[0] if refs and isinstance(refs[0], dict) else {}
        batch_id = first_ref.get("batch_id") or "latest"
        return f"process-product-outputs-{batch_id}"
    except Exception as exc:
        LOGGER.debug("Prefect runtime not available: %s", exc)
        return "product-output-process"


@task(
    name="product-output-process",
    task_run_name=_process_products_task_run_name,
    retries=0,
    timeout_seconds=None,
    tags=["prompt-output", "product", "post-process"],
    cache_result_in_memory=False,
)
def product_output_process_task(
    *,
    product_output_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Persist captured product flyouts for saved prompt outputs.

    Product HTML can be large, so logs only include aggregate counts. The raw
    payload is saved to Supabase in prompts_outputs_products.
    """
    task_logger = get_run_logger()
    refs = product_output_refs or []
    product_count = sum(len(ref.get("products") or []) for ref in refs if isinstance(ref, dict))
    task_logger.info("Starting product output process task. output_refs=%s product_count=%s", len(refs), product_count)
    settings = Settings.from_env(require_api_key=True)
    result = process_product_outputs(settings=settings, product_output_refs=refs)
    payload = asdict(result)
    task_logger.info("Finished product output process task: %s", payload)
    if result.failed_count:
        task_logger.warning("Product output processing completed with %s failure(s).", result.failed_count)
    return payload


def _process_entities_task_run_name() -> str:
    try:
        from prefect.runtime import task_run

        params = getattr(task_run, "parameters", None) or {}
        refs = params.get("entity_output_refs") or []
        first_ref = refs[0] if refs and isinstance(refs[0], dict) else {}
        batch_id = first_ref.get("batch_id") or "latest"
        return f"process-entity-outputs-{batch_id}"
    except Exception as exc:
        LOGGER.debug("Prefect runtime not available: %s", exc)
        return "entity-output-process"


@task(
    name="entity-output-process",
    task_run_name=_process_entities_task_run_name,
    retries=0,
    timeout_seconds=None,
    tags=["prompt-output", "entity", "post-process"],
    cache_result_in_memory=False,
)
def entity_output_process_task(
    *,
    entity_output_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Persist captured entity flyouts for saved prompt outputs.
    """
    task_logger = get_run_logger()
    refs = entity_output_refs or []
    entity_count = sum(len(ref.get("entities") or []) for ref in refs if isinstance(ref, dict))
    task_logger.info("Starting entity output process task. output_refs=%s entity_count=%s", len(refs), entity_count)
    settings = Settings.from_env(require_api_key=True)
    result = process_entity_outputs(settings=settings, entity_output_refs=refs)
    payload = asdict(result)
    task_logger.info("Finished entity output process task: %s", payload)
    if result.failed_count:
        task_logger.warning("Entity output processing completed with %s failure(s).", result.failed_count)
    return payload


def _score_workflow_task_run_name() -> str:
    try:
        from prefect.runtime import task_run

        params = getattr(task_run, "parameters", None) or {}
        outputs = params.get("saved_outputs") or []
        first_output = outputs[0] if outputs and isinstance(outputs[0], dict) else {}
        batch_id = first_output.get("batch_id") or "latest"
        return f"trigger-score-workflow-{batch_id}"
    except Exception as exc:
        LOGGER.debug("Prefect runtime not available: %s", exc)
        return "trigger-score-workflow"


@task(
    name="trigger-score-workflow",
    task_run_name=_score_workflow_task_run_name,
    retries=0,
    timeout_seconds=None,
    tags=["workflow", "score", "post-process"],
    cache_result_in_memory=False,
)
def score_workflow_trigger_task(
    *,
    saved_outputs: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Trigger the downstream score-single-output workflow for saved prompt outputs.
    """
    task_logger = get_run_logger()
    task_logger.info("Starting score workflow trigger task. saved_outputs=%s force=%s", len(saved_outputs or []), force)
    settings = Settings.from_env(require_api_key=True)
    result = trigger_score_workflows(settings=settings, saved_outputs=saved_outputs, force=force)
    payload = asdict(result)
    task_logger.info("Finished score workflow trigger task: %s", payload)
    if result.failed_count:
        task_logger.warning("Score workflow trigger completed with %s failure(s).", result.failed_count)
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
