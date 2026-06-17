from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api_client import ApiClient
from .chatgpt_runner import ChatGPTRunner
from .claude_runner import ClaudeRunner
from .config import Settings
from .google_ai_mode_runner import GoogleAIModeRunner
from .google_ai_overview_runner import GoogleAIOverviewRunner
from .google_suggestions_runner import capture_people_also_ask

LOGGER = logging.getLogger(__name__)

# Stable identifier for this worker process.
# Fly.io sets FLY_MACHINE_ID on every machine; fall back to hostname then a
# per-process UUID so local runs also get a unique ID.
WORKER_ID: str = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or str(_uuid.uuid4())[:12]


@dataclass(frozen=True)
class ExtractionRunResult:
    status: str
    loaded_count: int
    attempted_count: int
    saved_count: int
    skipped_count: int
    failed_count: int
    batch_id: str | None
    brand_id: str | None
    failures: list[dict[str, Any]]
    saved_outputs: list[dict[str, Any]]
    product_outputs: list[dict[str, Any]]
    entity_outputs: list[dict[str, Any]]


def run_extraction_job(
    *,
    settings: Settings,
    batch_id: str | None = None,
    prompts_file: Path | None = None,
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
) -> ExtractionRunResult:
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
        prompt_output_suggestions_table=settings.prompt_output_suggestions_table,
    )
    prompts, resolved_batch_id, resolved_brand_id = load_prompt_work(
        api=api,
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        only_remaining=not force_rerun,
        llm_model_filter=llm_model_filter,
    )
    prompts = prompts[max(0, skip) :]
    if limit:
        prompts = prompts[:limit]

    # Read required_models from batch config so per-prompt skip check is model-aware.
    required_models: list[str] | None = None
    if resolved_batch_id:
        try:
            batch = api.get_batch(resolved_batch_id)
            llm_model_config = batch.get("llm_models") or {}
            raw = llm_model_config.get("required_models") if isinstance(llm_model_config, dict) else None
            if isinstance(raw, list) and raw:
                required_models = [str(m) for m in raw]
        except Exception:
            pass

    LOGGER.info(
        "Loaded %s prompt(s). batch_id=%s brand_id=%s dry_run=%s only_remaining=%s llm_model_filter=%s",
        len(prompts),
        resolved_batch_id or "local",
        resolved_brand_id or "mixed",
        dry_run,
        not force_rerun,
        llm_model_filter or "any",
    )

    if dry_run:
        for prompt in prompts[:5]:
            LOGGER.info(
                "Dry run prompt: id=%s brand_id=%s text=%r",
                prompt.get("id"),
                prompt.get("brand_id"),
                prompt_text(prompt)[:120],
            )
        return ExtractionRunResult(
            status="dry_run",
            loaded_count=len(prompts),
            attempted_count=0,
            saved_count=0,
            skipped_count=0,
            failed_count=0,
            batch_id=resolved_batch_id,
            brand_id=resolved_brand_id,
            failures=[],
            saved_outputs=[],
            product_outputs=[],
            entity_outputs=[],
        )

    saved_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []
    saved_outputs: list[dict[str, Any]] = []
    product_outputs: list[dict[str, Any]] = []
    entity_outputs: list[dict[str, Any]] = []

    resolved_auto_login = settings.auto_login if auto_login is None else auto_login
    resolved_login_email = login_email or settings.login_email
    resolved_chrome_user_data_dir = chrome_user_data_dir or settings.chrome_user_data_dir
    LOGGER.info(
        "Starting ChatGPT browser session. chrome_user_data_dir=%s headless=%s auto_login=%s login_email=%s",
        resolved_chrome_user_data_dir,
        headless if headless is not None else settings.headless,
        resolved_auto_login,
        resolved_login_email or "<unset>",
    )

    chrome_profile_index: str | None = os.environ.get("CHROME_PROFILE_INDEX") or None
    # Email set by entrypoint.sh after acquiring a profile slot from chatgpt_profiles table.
    chrome_profile_email: str | None = os.environ.get("CHATGPT_LOGIN_EMAIL") or None

    with ChatGPTRunner(
        settings.chatgpt_url,
        headless=headless if headless is not None else settings.headless,
        chrome_user_data_dir=resolved_chrome_user_data_dir,
        login_wait_seconds=settings.login_wait_seconds,
        response_timeout_seconds=settings.response_timeout_seconds,
        sources_panel_pause_seconds=sources_panel_pause_seconds,
        auto_login=resolved_auto_login,
        accounts=settings.accounts,
        login_email=resolved_login_email,
    ) as runner:
        # Detect login state and signed-in account once per session.
        session_info = runner.get_session_info()
        # Enrich with the email from the chatgpt_profiles DB (set by entrypoint).
        # This fills chatgpt_account in output metadata without needing DOM scraping.
        if chrome_profile_email:
            session_info = {**session_info, "account_name": chrome_profile_email}
        LOGGER.info(
            "ChatGPT session info. logged_in=%s account_email=%r chrome_profile_index=%s chrome_user_data_dir=%s",
            session_info.get("logged_in"),
            session_info.get("account_name") or "<unknown>",
            chrome_profile_index or "<unset>",
            resolved_chrome_user_data_dir,
        )

        for index, prompt in enumerate(prompts, start=1):
            prompt_id = str(prompt.get("id") or "")
            prompt_brand_id = str(prompt.get("brand_id") or resolved_brand_id or "")
            if not prompt_id or not prompt_brand_id:
                skipped_count += 1
                LOGGER.warning("Skipping prompt missing id or brand_id: %s", prompt)
                continue

            existing_output = (
                None
                if force_rerun
                else api.find_existing_prompt_output(
                    prompt_id,
                    prompt_brand_id,
                    resolved_batch_id,
                    llm_model_filter=llm_model_filter,
                    required_models=required_models,
                )
            )
            if existing_output:
                skipped_count += 1
                LOGGER.info(
                    "[%s/%s] Skipping existing output for prompt %s. output_id=%s llm_model=%s run_at=%s force_rerun=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    existing_output.get("output_id") or existing_output.get("id"),
                    existing_output.get("llm_model"),
                    existing_output.get("run_at"),
                    force_rerun,
                )
                continue

            # Claim the prompt so no other worker starts processing it concurrently.
            # Skipped when force_rerun=True (intentional re-processing).
            if not force_rerun:
                claimed = api.try_claim_prompt(
                    prompt_id,
                    resolved_batch_id,
                    prompt_brand_id,
                    llm_model_filter or "gpt",
                    WORKER_ID,
                )
                if not claimed:
                    LOGGER.info(
                        "[%s/%s] Prompt %s already claimed by another worker — skipping.",
                        index,
                        len(prompts),
                        prompt_id,
                    )
                    skipped_count += 1
                    continue

            text = prompt_text(prompt)
            LOGGER.info("[%s/%s] Running prompt %s", index, len(prompts), prompt_id)

            try:
                capture = runner.run_prompt(text)
                output = build_prompt_output(
                    prompt,
                    capture.response,
                    capture.markdown,
                    capture.capture_method,
                    capture.markdown_capture_method,
                    capture.raw_html,
                    capture.raw_html_capture_method,
                    capture.llm_model,
                    capture.url,
                    resolved_batch_id,
                    capture.sources,
                    capture.source_capture_method,
                    session_info=session_info,
                    chrome_profile_index=chrome_profile_index,
                    chrome_user_data_dir=resolved_chrome_user_data_dir,
                )
                LOGGER.info(
                    "[%s/%s] Core capture summary for prompt %s: response_length=%s markdown_length=%s markdown_method=%s raw_html_length=%s llm_model=%s source_count=%s source_method=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    len(capture.response or ""),
                    len(capture.markdown or ""),
                    capture.markdown_capture_method,
                    len(capture.raw_html or ""),
                    capture.llm_model,
                    len(capture.sources or []),
                    capture.source_capture_method,
                )
                # Re-check immediately before saving — another worker may have saved
                # this specific model while Chrome was running it.
                # Use the exact captured model (not required_models) so we don't
                # create duplicate rows for a model we already have.
                concurrent_output = (
                    None
                    if force_rerun
                    else api.find_existing_prompt_output(
                        prompt_id,
                        prompt_brand_id,
                        resolved_batch_id,
                        llm_model_filter=capture.llm_model,
                        required_models=None,
                    )
                )
                if concurrent_output:
                    skipped_count += 1
                    LOGGER.warning(
                        "[%s/%s] Concurrent worker already saved prompt %s — discarding our result. output_id=%s",
                        index,
                        len(prompts),
                        prompt_id,
                        concurrent_output.get("output_id") or concurrent_output.get("id"),
                    )
                    continue

                saved = api.save_prompt_output(output)
                saved_count += 1
                saved_output = normalize_saved_output(saved, output)
                saved_outputs.append(saved_output)

                LOGGER.info(
                    "[%s/%s] Saved core prompt output for prompt %s before flyout extraction. output_id=%s response=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    saved_output.get("output_id"),
                    saved or "ok",
                )

                products = []
                if capture_products:
                    products = runner.capture_product_flyouts()
                product_capture_method = (
                    "product_flyouts" if products else ("skipped" if not capture_products else "none")
                )
                LOGGER.info(
                    "[%s/%s] Product capture for prompt %s: enabled=%s count=%s method=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    capture_products,
                    len(products),
                    product_capture_method,
                )
                if products:
                    product_outputs.append(
                        {
                            **saved_output,
                            "products": products,
                        }
                    )

                entities = []
                if capture_entities:
                    entities = runner.capture_entity_flyouts()
                entity_capture_method = (
                    "entity_flyouts" if entities else ("skipped" if not capture_entities else "none")
                )
                LOGGER.info(
                    "[%s/%s] Entity capture for prompt %s: enabled=%s count=%s method=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    capture_entities,
                    len(entities),
                    entity_capture_method,
                )
                if entities:
                    entity_outputs.append(
                        {
                            **saved_output,
                            "entities": entities,
                        }
                    )
                try:
                    summary_patch = build_flyout_summary_patch(
                        output,
                        products,
                        product_capture_method,
                        entities,
                        entity_capture_method,
                    )
                    api.update_prompt_output(saved_output, summary_patch)
                    LOGGER.info(
                        "[%s/%s] Updated prompt output flyout summary metadata. output_id=%s product_count=%s entity_count=%s",
                        index,
                        len(prompts),
                        saved_output.get("output_id"),
                        len(products),
                        len(entities),
                    )
                except Exception as summary_exc:
                    LOGGER.warning(
                        "[%s/%s] Could not update flyout summary metadata for prompt %s output_id=%s: %s",
                        index,
                        len(prompts),
                        prompt_id,
                        saved_output.get("output_id"),
                        summary_exc,
                    )
                LOGGER.info(
                    "[%s/%s] Completed prompt %s extraction bundle. output_id=%s product_count=%s entity_count=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    saved_output.get("output_id"),
                    len(products),
                    len(entities),
                )
                if not force_rerun:
                    api.complete_claim(prompt_id, resolved_batch_id, llm_model_filter or "gpt")
            except Exception as exc:
                failed_count += 1
                failure = {"prompt_id": prompt_id, "brand_id": prompt_brand_id, "error": str(exc)}
                failures.append(failure)
                LOGGER.exception("[%s/%s] Prompt %s failed: %s", index, len(prompts), prompt_id, exc)
                if not force_rerun:
                    api.release_claim(
                        prompt_id, resolved_batch_id, llm_model_filter or "gpt", error_message=str(exc)[:500]
                    )

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return ExtractionRunResult(
        status=status,
        loaded_count=len(prompts),
        attempted_count=len(prompts) - skipped_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        batch_id=resolved_batch_id,
        brand_id=resolved_brand_id,
        failures=failures,
        saved_outputs=saved_outputs,
        product_outputs=product_outputs,
        entity_outputs=entity_outputs,
    )


def run_google_ai_mode_extraction_job(
    *,
    settings: Settings,
    batch_id: str | None = None,
    prompts_file: Path | None = None,
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
) -> ExtractionRunResult:
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
        prompt_output_suggestions_table=settings.prompt_output_suggestions_table,
    )
    prompts, resolved_batch_id, resolved_brand_id = load_prompt_work(
        api=api,
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        only_remaining=not force_rerun,
        llm_model_filter=llm_model_filter,
    )
    prompts = prompts[max(0, skip) :]
    if limit:
        prompts = prompts[:limit]

    LOGGER.info(
        "Loaded %s prompt(s) for Google AI Mode. batch_id=%s brand_id=%s dry_run=%s only_remaining=%s llm_model_filter=%s",
        len(prompts),
        resolved_batch_id or "local",
        resolved_brand_id or "mixed",
        dry_run,
        not force_rerun,
        llm_model_filter or "any",
    )

    if dry_run:
        for prompt in prompts[:5]:
            LOGGER.info("Dry run Google AI Mode prompt: id=%s text=%r", prompt.get("id"), prompt_text(prompt)[:120])
        return ExtractionRunResult(
            status="dry_run",
            loaded_count=len(prompts),
            attempted_count=0,
            saved_count=0,
            skipped_count=0,
            failed_count=0,
            batch_id=resolved_batch_id,
            brand_id=resolved_brand_id,
            failures=[],
            saved_outputs=[],
            product_outputs=[],
            entity_outputs=[],
        )

    saved_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []
    saved_outputs: list[dict[str, Any]] = []

    resolved_chrome_user_data_dir = chrome_user_data_dir or settings.google_chrome_user_data_dir
    resolved_country = country or settings.google_country
    resolved_language = language or settings.google_language
    from .google_chrome_factory import resolve_proxy_url

    proxy_url = resolve_proxy_url(use_proxy)
    LOGGER.info(
        "Starting Google AI Mode browser session. chrome_user_data_dir=%s headless=%s country=%s language=%s proxy=%s",
        resolved_chrome_user_data_dir,
        headless if headless is not None else settings.headless,
        resolved_country or "<default>",
        resolved_language,
        "yes" if proxy_url else "no",
    )

    with GoogleAIModeRunner(
        settings.google_url,
        headless=headless if headless is not None else settings.headless,
        chrome_user_data_dir=resolved_chrome_user_data_dir,
        response_timeout_seconds=settings.response_timeout_seconds,
        country=resolved_country,
        language=resolved_language,
        use_ai_mode_param=settings.google_use_ai_mode_param,
        use_advanced_ai_param=settings.google_use_advanced_ai_param,
        proxy_url=proxy_url,
    ) as runner:
        for index, prompt in enumerate(prompts, start=1):
            prompt_id = str(prompt.get("id") or "")
            prompt_brand_id = str(prompt.get("brand_id") or resolved_brand_id or "")
            if not prompt_id or not prompt_brand_id:
                skipped_count += 1
                LOGGER.warning("Skipping Google AI Mode prompt missing id or brand_id: %s", prompt)
                continue

            existing_output = (
                None
                if force_rerun
                else api.find_existing_prompt_output(
                    prompt_id,
                    prompt_brand_id,
                    resolved_batch_id,
                    llm_model_filter=llm_model_filter,
                )
            )
            if existing_output:
                skipped_count += 1
                LOGGER.info(
                    "[%s/%s] Skipping existing Google AI Mode output for prompt %s. output_id=%s llm_model=%s run_at=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    existing_output.get("output_id") or existing_output.get("id"),
                    existing_output.get("llm_model"),
                    existing_output.get("run_at"),
                )
                continue

            if not force_rerun:
                claimed = api.try_claim_prompt(
                    prompt_id,
                    resolved_batch_id,
                    prompt_brand_id,
                    llm_model_filter or "google-ai-mode",
                    WORKER_ID,
                )
                if not claimed:
                    LOGGER.info(
                        "[%s/%s] Google AI Mode prompt %s already claimed by another worker — skipping.",
                        index,
                        len(prompts),
                        prompt_id,
                    )
                    skipped_count += 1
                    continue

            text = prompt_text(prompt)
            LOGGER.info("[%s/%s] Running Google AI Mode prompt %s", index, len(prompts), prompt_id)
            try:
                capture = runner.run_prompt(text)
                output = build_google_ai_mode_prompt_output(
                    prompt,
                    capture.response,
                    capture.markdown,
                    capture.capture_method,
                    capture.markdown_capture_method,
                    capture.raw_html,
                    capture.raw_html_capture_method,
                    capture.llm_model,
                    capture.url,
                    resolved_batch_id,
                    capture.sources,
                    capture.source_capture_method,
                    ai_mode_triggered=capture.ai_mode_triggered,
                    capture_state=capture.capture_state,
                    error=capture.error,
                    country=resolved_country,
                    language=resolved_language,
                )
                # Record proxy bytes for cost attribution.
                proxy_bytes = runner.browser.take_proxy_bytes() if runner.browser else 0
                if isinstance(output.get("output_metadata"), dict):
                    output["output_metadata"]["proxy_usage"] = {
                        "bytes_transferred": proxy_bytes,
                        "use_proxy": bool(proxy_url),
                        "provider": "dataimpulse" if proxy_url else None,
                    }
                saved = api.save_prompt_output(output)
                saved_count += 1
                saved_output = normalize_saved_output(saved, output)
                saved_outputs.append(saved_output)
                LOGGER.info(
                    "[%s/%s] Saved Google AI Mode output for prompt %s. output_id=%s triggered=%s response_length=%s source_count=%s state=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    saved_output.get("output_id"),
                    capture.ai_mode_triggered,
                    len(capture.response or ""),
                    len(capture.sources or []),
                    capture.capture_state,
                )
                suggestion_count = _capture_and_save_suggestions(
                    api=api,
                    driver=runner.driver,
                    saved_output=saved_output,
                    prompt=prompt,
                    batch_id=resolved_batch_id,
                    llm_model="google-ai-mode",
                    index=index,
                    total=len(prompts),
                )
                if suggestion_count > 0:
                    current_metadata = (
                        output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
                    )
                    try:
                        api.update_prompt_output(
                            saved_output,
                            {
                                "output_metadata": {**current_metadata, "suggestion_count": suggestion_count},
                            },
                        )
                    except Exception as patch_exc:
                        LOGGER.warning(
                            "[%s/%s] Could not patch suggestion_count for prompt %s: %s",
                            index,
                            len(prompts),
                            prompt_id,
                            patch_exc,
                        )
            except Exception as exc:
                failed_count += 1
                failure = {"prompt_id": prompt_id, "brand_id": prompt_brand_id, "error": str(exc)}
                failures.append(failure)
                LOGGER.exception("[%s/%s] Google AI Mode prompt %s failed: %s", index, len(prompts), prompt_id, exc)
                if not force_rerun:
                    api.release_claim(
                        prompt_id, resolved_batch_id, llm_model_filter or "google-ai-mode", error_message=str(exc)[:500]
                    )
            else:
                if not force_rerun:
                    api.complete_claim(prompt_id, resolved_batch_id, llm_model_filter or "google-ai-mode")

            if index < len(prompts):
                delay = random.uniform(3.0, 7.0)
                LOGGER.info("[%s/%s] Pausing %.1fs before next prompt.", index, len(prompts), delay)
                time.sleep(delay)

        if debug_pause_seconds > 0:
            LOGGER.info("Debug pause: browser staying open for %s seconds. Inspect at will.", debug_pause_seconds)
            time.sleep(debug_pause_seconds)

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return ExtractionRunResult(
        status=status,
        loaded_count=len(prompts),
        attempted_count=len(prompts) - skipped_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        batch_id=resolved_batch_id,
        brand_id=resolved_brand_id,
        failures=failures,
        saved_outputs=saved_outputs,
        product_outputs=[],
        entity_outputs=[],
    )


def run_google_ai_overview_extraction_job(
    *,
    settings: Settings,
    batch_id: str | None = None,
    prompts_file: Path | None = None,
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
    paa_titles_only: bool = True,
) -> ExtractionRunResult:
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
        prompt_output_suggestions_table=settings.prompt_output_suggestions_table,
    )
    prompts, resolved_batch_id, resolved_brand_id = load_prompt_work(
        api=api,
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        only_remaining=not force_rerun,
        llm_model_filter=llm_model_filter,
    )
    prompts = prompts[max(0, skip) :]
    if limit:
        prompts = prompts[:limit]

    LOGGER.info(
        "Loaded %s prompt(s) for Google AI Overview. batch_id=%s brand_id=%s dry_run=%s only_remaining=%s llm_model_filter=%s",
        len(prompts),
        resolved_batch_id or "local",
        resolved_brand_id or "mixed",
        dry_run,
        not force_rerun,
        llm_model_filter or "any",
    )

    if dry_run:
        for prompt in prompts[:5]:
            LOGGER.info("Dry run Google AI Overview prompt: id=%s text=%r", prompt.get("id"), prompt_text(prompt)[:120])
        return ExtractionRunResult(
            status="dry_run",
            loaded_count=len(prompts),
            attempted_count=0,
            saved_count=0,
            skipped_count=0,
            failed_count=0,
            batch_id=resolved_batch_id,
            brand_id=resolved_brand_id,
            failures=[],
            saved_outputs=[],
            product_outputs=[],
            entity_outputs=[],
        )

    saved_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []
    saved_outputs: list[dict[str, Any]] = []

    resolved_chrome_user_data_dir = chrome_user_data_dir or settings.google_chrome_user_data_dir
    resolved_country = country or settings.google_country
    resolved_language = language or settings.google_language
    from .google_chrome_factory import resolve_proxy_url

    proxy_url = resolve_proxy_url(use_proxy)
    LOGGER.info(
        "Starting Google AI Overview browser session. chrome_user_data_dir=%s headless=%s country=%s language=%s proxy=%s",
        resolved_chrome_user_data_dir,
        headless if headless is not None else settings.headless,
        resolved_country or "<default>",
        resolved_language,
        "yes" if proxy_url else "no",
    )

    with GoogleAIOverviewRunner(
        settings.google_url,
        headless=headless if headless is not None else settings.headless,
        chrome_user_data_dir=resolved_chrome_user_data_dir,
        response_timeout_seconds=settings.response_timeout_seconds,
        country=resolved_country,
        language=resolved_language,
        proxy_url=proxy_url,
    ) as runner:
        for index, prompt in enumerate(prompts, start=1):
            prompt_id = str(prompt.get("id") or "")
            prompt_brand_id = str(prompt.get("brand_id") or resolved_brand_id or "")
            if not prompt_id or not prompt_brand_id:
                skipped_count += 1
                LOGGER.warning("Skipping Google AI Overview prompt missing id or brand_id: %s", prompt)
                continue

            existing_output = (
                None
                if force_rerun
                else api.find_existing_prompt_output(
                    prompt_id,
                    prompt_brand_id,
                    resolved_batch_id,
                    llm_model_filter=llm_model_filter,
                )
            )
            if existing_output:
                skipped_count += 1
                LOGGER.info(
                    "[%s/%s] Skipping existing Google AI Overview output for prompt %s. output_id=%s llm_model=%s run_at=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    existing_output.get("output_id") or existing_output.get("id"),
                    existing_output.get("llm_model"),
                    existing_output.get("run_at"),
                )
                continue

            if not force_rerun:
                claimed = api.try_claim_prompt(
                    prompt_id,
                    resolved_batch_id,
                    prompt_brand_id,
                    llm_model_filter or "google-ai-overview",
                    WORKER_ID,
                )
                if not claimed:
                    LOGGER.info(
                        "[%s/%s] Google AI Overview prompt %s already claimed by another worker — skipping.",
                        index,
                        len(prompts),
                        prompt_id,
                    )
                    skipped_count += 1
                    continue

            text = prompt_text(prompt)
            LOGGER.info("[%s/%s] Running Google AI Overview prompt %s", index, len(prompts), prompt_id)
            try:
                capture = runner.run_prompt(text)
                output = build_google_ai_overview_prompt_output(
                    prompt,
                    capture.response,
                    capture.markdown,
                    capture.capture_method,
                    capture.markdown_capture_method,
                    capture.raw_html,
                    capture.raw_html_capture_method,
                    capture.llm_model,
                    capture.url,
                    resolved_batch_id,
                    capture.sources,
                    capture.source_capture_method,
                    ai_overview_triggered=capture.ai_overview_triggered,
                    capture_state=capture.capture_state,
                    error=capture.error,
                    country=resolved_country,
                    language=resolved_language,
                )
                # Record proxy bytes for cost attribution.
                proxy_bytes = runner.browser.take_proxy_bytes() if runner.browser else 0
                if isinstance(output.get("output_metadata"), dict):
                    output["output_metadata"]["proxy_usage"] = {
                        "bytes_transferred": proxy_bytes,
                        "use_proxy": bool(proxy_url),
                        "provider": "dataimpulse" if proxy_url else None,
                    }
                saved = api.save_prompt_output(output)
                saved_count += 1
                saved_output = normalize_saved_output(saved, output)
                saved_outputs.append(saved_output)
                LOGGER.info(
                    "[%s/%s] Saved Google AI Overview output for prompt %s. output_id=%s triggered=%s response_length=%s source_count=%s state=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    saved_output.get("output_id"),
                    capture.ai_overview_triggered,
                    len(capture.response or ""),
                    len(capture.sources or []),
                    capture.capture_state,
                )
                suggestion_count = _capture_and_save_suggestions(
                    api=api,
                    driver=runner.driver,
                    saved_output=saved_output,
                    prompt=prompt,
                    batch_id=resolved_batch_id,
                    llm_model="google-ai-overview",
                    index=index,
                    total=len(prompts),
                    paa_titles_only=paa_titles_only,
                )
                if suggestion_count > 0:
                    current_metadata = (
                        output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
                    )
                    try:
                        api.update_prompt_output(
                            saved_output,
                            {
                                "output_metadata": {**current_metadata, "suggestion_count": suggestion_count},
                            },
                        )
                    except Exception as patch_exc:
                        LOGGER.warning(
                            "[%s/%s] Could not patch suggestion_count for prompt %s: %s",
                            index,
                            len(prompts),
                            prompt_id,
                            patch_exc,
                        )
            except Exception as exc:
                failed_count += 1
                failure = {"prompt_id": prompt_id, "brand_id": prompt_brand_id, "error": str(exc)}
                failures.append(failure)
                LOGGER.exception("[%s/%s] Google AI Overview prompt %s failed: %s", index, len(prompts), prompt_id, exc)
                if not force_rerun:
                    api.release_claim(
                        prompt_id,
                        resolved_batch_id,
                        llm_model_filter or "google-ai-overview",
                        error_message=str(exc)[:500],
                    )
            else:
                if not force_rerun:
                    api.complete_claim(prompt_id, resolved_batch_id, llm_model_filter or "google-ai-overview")

            if index < len(prompts):
                delay = random.uniform(3.0, 7.0)
                LOGGER.info("[%s/%s] Pausing %.1fs before next prompt.", index, len(prompts), delay)
                time.sleep(delay)

        if debug_pause_seconds > 0:
            LOGGER.info("Debug pause: browser staying open for %s seconds. Inspect at will.", debug_pause_seconds)
            time.sleep(debug_pause_seconds)

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return ExtractionRunResult(
        status=status,
        loaded_count=len(prompts),
        attempted_count=len(prompts) - skipped_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        batch_id=resolved_batch_id,
        brand_id=resolved_brand_id,
        failures=failures,
        saved_outputs=saved_outputs,
        product_outputs=[],
        entity_outputs=[],
    )


def run_claude_extraction_job(
    *,
    settings: Settings,
    batch_id: str | None = None,
    prompts_file: Path | None = None,
    brand_id: str | None = None,
    limit: int | None = None,
    skip: int = 0,
    dry_run: bool = False,
    headless: bool | None = None,
    chrome_user_data_dir: str | None = None,
    force_rerun: bool = False,
    llm_model_filter: str | None = "claude",
) -> ExtractionRunResult:
    if not batch_id and not prompts_file:
        raise ValueError("one of batch_id or prompts_file is required")

    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
        prompt_output_suggestions_table=settings.prompt_output_suggestions_table,
    )
    prompts, resolved_batch_id, resolved_brand_id = load_prompt_work(
        api=api,
        batch_id=batch_id,
        prompts_file=prompts_file,
        brand_id=brand_id,
        only_remaining=not force_rerun,
        llm_model_filter=llm_model_filter,
    )

    if skip:
        prompts = prompts[skip:]
    if limit is not None:
        prompts = prompts[:limit]
    if not prompts:
        LOGGER.info("No Claude prompts to process. batch_id=%s brand_id=%s", resolved_batch_id, resolved_brand_id)
        return ExtractionRunResult(
            status="no_prompts",
            loaded_count=0,
            attempted_count=0,
            saved_count=0,
            skipped_count=0,
            failed_count=0,
            batch_id=resolved_batch_id,
            brand_id=resolved_brand_id,
            failures=[],
            saved_outputs=[],
            product_outputs=[],
            entity_outputs=[],
        )

    saved_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []
    saved_outputs: list[dict[str, Any]] = []

    resolved_chrome_user_data_dir = chrome_user_data_dir or settings.claude_chrome_user_data_dir
    LOGGER.info(
        "Starting Claude browser session. chrome_user_data_dir=%s headless=%s",
        resolved_chrome_user_data_dir,
        headless if headless is not None else settings.headless,
    )

    with ClaudeRunner(
        settings.claude_url,
        headless=headless if headless is not None else settings.headless,
        chrome_user_data_dir=resolved_chrome_user_data_dir,
        login_wait_seconds=settings.login_wait_seconds,
        response_timeout_seconds=settings.response_timeout_seconds,
    ) as runner:
        session_info: dict[str, Any] = {"logged_in": True}

        for index, prompt in enumerate(prompts, start=1):
            prompt_id = str(prompt.get("id") or "")
            prompt_brand_id = str(prompt.get("brand_id") or resolved_brand_id or "")
            if not prompt_id or not prompt_brand_id:
                skipped_count += 1
                LOGGER.warning("Skipping prompt missing id or brand_id: %s", prompt)
                continue

            existing_output = (
                None
                if force_rerun
                else api.find_existing_prompt_output(
                    prompt_id,
                    prompt_brand_id,
                    resolved_batch_id,
                    llm_model_filter=llm_model_filter,
                )
            )
            if existing_output:
                skipped_count += 1
                LOGGER.info(
                    "[%s/%s] Skipping existing Claude output for prompt %s. output_id=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    existing_output.get("output_id") or existing_output.get("id"),
                )
                continue

            if not force_rerun:
                claimed = api.try_claim_prompt(
                    prompt_id,
                    resolved_batch_id,
                    prompt_brand_id,
                    llm_model_filter or "claude",
                    WORKER_ID,
                )
                if not claimed:
                    LOGGER.info(
                        "[%s/%s] Prompt %s already claimed by another worker — skipping.",
                        index,
                        len(prompts),
                        prompt_id,
                    )
                    skipped_count += 1
                    continue

            text = prompt_text(prompt)
            LOGGER.info("[%s/%s] Running Claude prompt %s", index, len(prompts), prompt_id)

            try:
                if dry_run:
                    LOGGER.info("[%s/%s] Dry run — skipping browser call.", index, len(prompts))
                    skipped_count += 1
                    if not force_rerun:
                        api.release_claim(prompt_id, resolved_batch_id, llm_model_filter or "claude")
                    continue

                capture = runner.run_prompt(text)
                output = build_claude_prompt_output(
                    prompt,
                    capture.response,
                    capture.markdown,
                    capture.capture_method,
                    capture.markdown_capture_method,
                    capture.raw_html,
                    capture.raw_html_capture_method,
                    capture.llm_model,
                    capture.url,
                    resolved_batch_id,
                    capture.sources,
                    capture.source_capture_method,
                    session_info=session_info,
                    chrome_user_data_dir=resolved_chrome_user_data_dir,
                )
                LOGGER.info(
                    "[%s/%s] Claude capture summary for prompt %s: response_length=%s markdown_length=%s llm_model=%s source_count=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    len(capture.response or ""),
                    len(capture.markdown or ""),
                    capture.llm_model,
                    len(capture.sources or []),
                )

                # Race-check before save
                concurrent_output = (
                    None
                    if force_rerun
                    else api.find_existing_prompt_output(
                        prompt_id,
                        prompt_brand_id,
                        resolved_batch_id,
                        llm_model_filter=capture.llm_model,
                    )
                )
                if concurrent_output:
                    skipped_count += 1
                    LOGGER.warning(
                        "[%s/%s] Concurrent worker already saved Claude prompt %s — discarding.",
                        index,
                        len(prompts),
                        prompt_id,
                    )
                    continue

                saved = api.save_prompt_output(output)
                saved_count += 1
                saved_output = normalize_saved_output(saved, output)
                saved_outputs.append(saved_output)
                LOGGER.info(
                    "[%s/%s] Saved Claude prompt output for prompt %s. output_id=%s",
                    index,
                    len(prompts),
                    prompt_id,
                    saved_output.get("output_id"),
                )

                if not force_rerun:
                    api.complete_claim(prompt_id, resolved_batch_id, llm_model_filter or "claude")

            except Exception as exc:
                failed_count += 1
                LOGGER.exception("[%s/%s] Failed Claude prompt %s: %s", index, len(prompts), prompt_id, exc)
                failures.append({"prompt_id": prompt_id, "brand_id": prompt_brand_id, "error": str(exc)})
                if not force_rerun:
                    try:
                        api.release_claim(prompt_id, resolved_batch_id, llm_model_filter or "claude")
                    except Exception:
                        pass

                delay = random.uniform(2, 5)
                LOGGER.info("Waiting %ss before next Claude prompt after failure.", round(delay, 1))
                time.sleep(delay)

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return ExtractionRunResult(
        status=status,
        loaded_count=len(prompts),
        attempted_count=len(prompts) - skipped_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        batch_id=resolved_batch_id,
        brand_id=resolved_brand_id,
        failures=failures,
        saved_outputs=saved_outputs,
        product_outputs=[],
        entity_outputs=[],
    )


def _normalise_claude_model(display_name: str) -> str:
    """
    Convert Claude's UI display name to a stable kebab-case slug.

    Examples:
      "Sonnet 4.6 Low"  -> "claude-sonnet-4-6"
      "Opus 4.8"        -> "claude-opus-4-8"
      "Haiku 4.5"       -> "claude-haiku-4-5"
      "claude-sonnet-4" -> "claude-sonnet-4"   (already normalised)
    """
    import re as _re

    if not display_name:
        return "claude"
    name = display_name.strip()
    # Already a proper slug — return as-is
    if name.lower().startswith("claude-"):
        return name.lower()
    # Strip trailing qualifiers like "Low", "High", "Fast", etc.
    name = _re.sub(r"\s+(Low|High|Fast|Slow|Extended|Preview)\s*$", "", name, flags=_re.IGNORECASE).strip()
    # "Sonnet 4.6" -> "claude-sonnet-4-6"
    slug = "claude-" + _re.sub(r"[\s.]+", "-", name.lower())
    return slug


def build_claude_prompt_output(
    prompt: dict[str, Any],
    response: str,
    markdown: str,
    capture_method: str,
    markdown_capture_method: str,
    raw_html: str,
    raw_html_capture_method: str,
    llm_model: str,
    url: str,
    batch_id: str | None,
    sources: list[dict[str, Any]] | None = None,
    source_capture_method: str = "none",
    session_info: dict[str, Any] | None = None,
    chrome_user_data_dir: str | None = None,
) -> dict[str, Any]:
    normalised_model = _normalise_claude_model(llm_model) if llm_model else "claude"
    output = build_prompt_output(
        prompt,
        response,
        markdown,
        capture_method,
        markdown_capture_method,
        raw_html,
        raw_html_capture_method,
        normalised_model,
        url,
        batch_id,
        sources,
        source_capture_method,
        session_info=session_info,
        chrome_user_data_dir=chrome_user_data_dir,
    )
    output["config"] = {
        **(output.get("config") or {}),
        "site": "Anthropic",
    }
    metadata = output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
    output["output_metadata"] = {
        **metadata,
        "llm_model": normalised_model,
        "site_used": "Anthropic",
    }
    return output


def load_prompt_work(
    *,
    api: ApiClient,
    batch_id: str | None,
    prompts_file: Path | None,
    brand_id: str | None,
    only_remaining: bool = True,
    llm_model_filter: str | None = "gpt",
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if prompts_file:
        with prompts_file.open("r", encoding="utf-8") as handle:
            prompts = json.load(handle)
        if not isinstance(prompts, list):
            raise RuntimeError(f"Expected a JSON array in {prompts_file}")
        resolved_brand_id = brand_id or first_brand_id(prompts)
        prompts = [with_brand_id(prompt, resolved_brand_id) for prompt in prompts]
        return prompts, None, resolved_brand_id

    if not batch_id:
        raise ValueError("batch_id is required when prompts_file is not provided")

    batch = api.get_batch(batch_id)
    resolved_brand_id = brand_id or batch.get("brand_id")
    if not resolved_brand_id:
        raise RuntimeError(f"Batch {batch_id} does not include brand_id")

    # Read required_models from batch config if present.
    llm_model_config = batch.get("llm_models") or {}
    required_models: list[str] | None = None
    if isinstance(llm_model_config, dict):
        raw = llm_model_config.get("required_models")
        if isinstance(raw, list) and raw:
            required_models = [str(m) for m in raw]
            LOGGER.info(
                "load_prompt_work: batch config specifies required_models=%s. batch_id=%s",
                required_models,
                batch_id,
            )

    return (
        api.get_prompts(
            batch_id,
            str(resolved_brand_id),
            only_remaining=only_remaining,
            llm_model_filter=llm_model_filter,
            required_models=required_models,
        ),
        batch_id,
        str(resolved_brand_id),
    )


def first_brand_id(prompts: list[dict[str, Any]]) -> str | None:
    for prompt in prompts:
        if prompt.get("brand_id"):
            return str(prompt["brand_id"])
    return None


def with_brand_id(prompt: dict[str, Any], brand_id: str | None) -> dict[str, Any]:
    if prompt.get("brand_id") or not brand_id:
        return prompt
    return {**prompt, "brand_id": brand_id}


def prompt_text(prompt: dict[str, Any]) -> str:
    text = prompt.get("text") or prompt.get("prompt")
    if not text:
        raise RuntimeError(f"Prompt missing text: {prompt}")
    return str(text)


def normalize_saved_output(saved: dict[str, Any] | None, original_output: dict[str, Any]) -> dict[str, Any]:
    saved_data = saved if isinstance(saved, dict) else {}
    nested_output = saved_data.get("output") if isinstance(saved_data.get("output"), dict) else {}
    merged = {**original_output, **nested_output, **saved_data}
    output_id = merged.get("id") or merged.get("output_id") or merged.get("prompt_output_id")
    return {
        "id": output_id,
        "output_id": output_id,
        "prompt_id": merged.get("prompt_id"),
        "brand_id": merged.get("brand_id"),
        "batch_id": merged.get("batch_id"),
    }


def build_prompt_output(
    prompt: dict[str, Any],
    response: str,
    markdown: str,
    capture_method: str,
    markdown_capture_method: str,
    raw_html: str,
    raw_html_capture_method: str,
    llm_model: str,
    url: str,
    batch_id: str | None,
    sources: list[dict[str, Any]] | None = None,
    source_capture_method: str = "none",
    products: list[dict[str, Any]] | None = None,
    product_capture_method: str = "none",
    entities: list[dict[str, Any]] | None = None,
    entity_capture_method: str = "none",
    session_info: dict[str, Any] | None = None,
    chrome_profile_index: str | None = None,
    chrome_user_data_dir: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "prompt_id": prompt.get("id"),
        "brand_id": prompt.get("brand_id"),
        "response": response,
        "markdown": markdown or None,
        "raw_html": raw_html,
        "sources": sources or [],
        "batch_id": batch_id or prompt.get("batch_id"),
        "llm_model": llm_model or "chatgpt",
        "config": {
            "site": "OpenAI",
            "category": prompt.get("category"),
            "tags": prompt.get("tags"),
            "measurements": prompt.get("measurements"),
        },
        "output_metadata": {
            "brand_name": (prompt.get("brand") or {}).get("name") if isinstance(prompt.get("brand"), dict) else None,
            "brand_description": (prompt.get("brand") or {}).get("description")
            if isinstance(prompt.get("brand"), dict)
            else None,
            "llm_model": llm_model or "chatgpt",
            "approved": prompt.get("approved"),
            "active": prompt.get("active"),
            "created_at": prompt.get("created_at"),
            "original_metadata": {
                "llm_model": llm_model or "chatgpt",
                "main_response_capture_method": capture_method,
                "markdown_capture_method": markdown_capture_method,
                "markdown_length": len(markdown or ""),
                "copy_validation_status": "validated" if capture_method.startswith("copy_button") else "fallback",
                "raw_html_capture_method": raw_html_capture_method,
                "raw_html_length": len(raw_html or ""),
                "url": url,
                "source_count": len(sources or []),
                "source_capture_method": source_capture_method,
                "product_count": len(products or []),
                "product_capture_method": product_capture_method,
                "product_extraction": {
                    "process_name": "product_extraction",
                    "product_count": len(products or []),
                    "capture_method": product_capture_method,
                },
                "entity_count": len(entities or []),
                "entity_capture_method": entity_capture_method,
                "entity_extraction": {
                    "process_name": "entity_extraction",
                    "entity_count": len(entities or []),
                    "capture_method": entity_capture_method,
                },
                # Session / profile metadata
                "logged_in": (session_info or {}).get("logged_in", False),
                "login_button_present": (session_info or {}).get("login_button_present", False),
                "chatgpt_account": (session_info or {}).get("account_name") or None,
                "chatgpt_account_label": (session_info or {}).get("account_label") or None,
                "chrome_profile_index": chrome_profile_index,
                "chrome_user_data_dir": chrome_user_data_dir,
            },
            "site_used": "OpenAI",
            "timestamp": now,
            "app_type": "automated_extraction",
            "app_version": "1.0.0",
            "prompt_source": "batch" if batch_id else "local",
            "worker_name": os.getenv("FLY_MACHINE_ID") or os.getenv("FLY_APP_NAME"),
            "worker_app": os.getenv("FLY_APP_NAME"),
            "worker_pool": os.getenv("PREFECT_WORK_POOL"),
            "suggestion_count": 0,
        },
    }


def build_google_ai_mode_prompt_output(
    prompt: dict[str, Any],
    response: str,
    markdown: str,
    capture_method: str,
    markdown_capture_method: str,
    raw_html: str,
    raw_html_capture_method: str,
    llm_model: str,
    url: str,
    batch_id: str | None,
    sources: list[dict[str, Any]] | None = None,
    source_capture_method: str = "none",
    *,
    ai_mode_triggered: bool,
    capture_state: str,
    error: str | None,
    country: str | None,
    language: str,
) -> dict[str, Any]:
    output = build_prompt_output(
        prompt,
        response,
        markdown,
        capture_method,
        markdown_capture_method,
        raw_html,
        raw_html_capture_method,
        llm_model or "google-ai-mode",
        url,
        batch_id,
        sources,
        source_capture_method,
    )
    output["config"] = {
        **(output.get("config") or {}),
        "site": "Google",
        "provider": "google-ai-mode",
        "country": country,
        "language": language,
    }
    metadata = output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
    original_metadata = metadata.get("original_metadata") if isinstance(metadata.get("original_metadata"), dict) else {}
    output["output_metadata"] = {
        **metadata,
        "llm_model": llm_model or "google-ai-mode",
        "site_used": "Google",
        "google_ai_mode": {
            "triggered": ai_mode_triggered,
            "capture_state": capture_state,
            "error": error,
            "country": country,
            "language": language,
            "url": url,
        },
        "original_metadata": {
            **original_metadata,
            "llm_model": llm_model or "google-ai-mode",
            "provider": "google-ai-mode",
            "site": "Google",
            "site_used": "Google",
            "ai_mode_triggered": ai_mode_triggered,
            "capture_state": capture_state,
            "capture_error": error,
            "google_country": country,
            "google_language": language,
        },
    }
    output["version_info"] = {
        **(output.get("version_info") or {}),
        "app_type": "automated_extraction_google_ai_mode",
    }
    return output


def build_google_ai_overview_prompt_output(
    prompt: dict[str, Any],
    response: str,
    markdown: str,
    capture_method: str,
    markdown_capture_method: str,
    raw_html: str,
    raw_html_capture_method: str,
    llm_model: str,
    url: str,
    batch_id: str | None,
    sources: list[dict[str, Any]] | None = None,
    source_capture_method: str = "none",
    *,
    ai_overview_triggered: bool,
    capture_state: str,
    error: str | None,
    country: str | None,
    language: str,
) -> dict[str, Any]:
    output = build_prompt_output(
        prompt,
        response,
        markdown,
        capture_method,
        markdown_capture_method,
        raw_html,
        raw_html_capture_method,
        llm_model or "google-ai-overview",
        url,
        batch_id,
        sources,
        source_capture_method,
    )
    output["config"] = {
        **(output.get("config") or {}),
        "site": "Google",
        "provider": "google-ai-overview",
        "country": country,
        "language": language,
    }
    metadata = output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
    original_metadata = metadata.get("original_metadata") if isinstance(metadata.get("original_metadata"), dict) else {}
    output["output_metadata"] = {
        **metadata,
        "llm_model": llm_model or "google-ai-overview",
        "site_used": "Google",
        "ai_overview": ai_overview_triggered,
        "google_ai_overview": {
            "triggered": ai_overview_triggered,
            "capture_state": capture_state,
            "error": error,
            "country": country,
            "language": language,
            "url": url,
        },
        "original_metadata": {
            **original_metadata,
            "llm_model": llm_model or "google-ai-overview",
            "provider": "google-ai-overview",
            "site": "Google",
            "site_used": "Google",
            "ai_overview": ai_overview_triggered,
            "ai_overview_triggered": ai_overview_triggered,
            "capture_state": capture_state,
            "capture_error": error,
            "google_country": country,
            "google_language": language,
        },
    }
    output["version_info"] = {
        **(output.get("version_info") or {}),
        "app_type": "automated_extraction_google_ai_overview",
    }
    return output


def _capture_and_save_suggestions(
    *,
    api: ApiClient,
    driver: Any,
    saved_output: dict[str, Any],
    prompt: dict[str, Any],
    batch_id: str | None,
    llm_model: str,
    index: int,
    total: int,
    paa_titles_only: bool = True,
) -> int:
    """Capture PAA suggestions from the current page and save them to Supabase.

    Returns the number of suggestions saved (0 if none found or on error).
    """
    if not driver:
        return 0
    output_id = saved_output.get("output_id") or saved_output.get("id")
    prompt_id = str(prompt.get("id") or "")
    brand_id = str(prompt.get("brand_id") or saved_output.get("brand_id") or "")
    resolved_batch = batch_id or str(prompt.get("batch_id") or "")
    if not output_id or not prompt_id or not brand_id:
        return 0
    try:
        paa = capture_people_also_ask(driver, titles_only=paa_titles_only)
        if not paa.suggestions:
            LOGGER.info("[%s/%s] No PAA suggestions found for prompt %s.", index, total, prompt_id)
            return 0

        rows = [
            {
                "output_id": int(output_id),
                "prompt_id": prompt_id,
                "brand_id": brand_id,
                "batch_id": resolved_batch,
                "index": s.index,
                "text": s.text,
                "response": s.response or None,
                "sources": s.sources or None,
                "raw_html": s.raw_html or None,
                "llm_model": llm_model,
                "capture_method": s.capture_method,
                "error": s.error or None,
                "metadata": {
                    "prompt_id": prompt_id,
                    "brand_id": brand_id,
                    "batch_id": resolved_batch,
                    "output_id": int(output_id),
                    "paa_total": paa.count,
                    "paa_capture_method": paa.capture_method,
                },
            }
            for s in paa.suggestions
        ]
        api.save_prompt_output_suggestions(rows)
        LOGGER.info(
            "[%s/%s] Saved %s PAA suggestion(s) for prompt %s. output_id=%s",
            index,
            total,
            len(rows),
            prompt_id,
            output_id,
        )
        return len(rows)
    except Exception as exc:
        LOGGER.warning(
            "[%s/%s] PAA suggestion capture/save failed for prompt %s: %s",
            index,
            total,
            prompt_id,
            exc,
        )
        return 0


def build_flyout_summary_patch(
    output: dict[str, Any],
    products: list[dict[str, Any]],
    product_capture_method: str,
    entities: list[dict[str, Any]],
    entity_capture_method: str,
) -> dict[str, Any]:
    metadata = output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
    original_metadata = metadata.get("original_metadata") if isinstance(metadata.get("original_metadata"), dict) else {}
    updated_metadata = {
        **metadata,
        "original_metadata": {
            **original_metadata,
            "product_count": len(products or []),
            "product_capture_method": product_capture_method,
            "product_extraction": {
                "process_name": "product_extraction",
                "product_count": len(products or []),
                "capture_method": product_capture_method,
            },
            "entity_count": len(entities or []),
            "entity_capture_method": entity_capture_method,
            "entity_extraction": {
                "process_name": "entity_extraction",
                "entity_count": len(entities or []),
                "capture_method": entity_capture_method,
            },
        },
    }
    return {"output_metadata": updated_metadata}
