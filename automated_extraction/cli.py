from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .entity_output_processor import process_entity_outputs
from .extraction import run_extraction_job
from .product_output_processor import process_product_outputs
from .workflow_trigger import trigger_score_workflows


LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    quiet_third_party_http_logs()

    settings = Settings.from_env(require_api_key=not args.login_only)

    if args.login_only:
        from .chatgpt_runner import ChatGPTRunner

        profile_dir = args.chrome_user_data_dir or settings.chrome_user_data_dir
        LOGGER.info("Opening ChatGPT login session with profile: %s", profile_dir)
        with ChatGPTRunner(
            settings.chatgpt_url,
            headless=False,
            chrome_user_data_dir=profile_dir,
            login_wait_seconds=settings.login_wait_seconds,
            response_timeout_seconds=settings.response_timeout_seconds,
            sources_panel_pause_seconds=args.sources_panel_pause_seconds,
        ):
            LOGGER.info("ChatGPT login is ready and stored in the profile.")
        return 0

    if not args.batch_id and not args.prompts_file:
        parser.error("one of --batch-id or --prompts-file is required unless --login-only is used")

    result = run_extraction_job(
        settings=settings,
        batch_id=args.batch_id,
        prompts_file=args.prompts_file,
        brand_id=args.brand_id,
        limit=args.limit,
        skip=args.skip,
        dry_run=args.dry_run,
        headless=args.headless if args.headless is not None else settings.headless,
        chrome_user_data_dir=args.chrome_user_data_dir,
        sources_panel_pause_seconds=args.sources_panel_pause_seconds,
        force_rerun=args.force_rerun,
        llm_model_filter=args.llm_model_filter,
    )
    payload = asdict(result)
    product_output_refs = payload.pop("product_outputs", []) or []
    entity_output_refs = payload.pop("entity_outputs", []) or []
    product_processing_result = None
    if not args.dry_run and product_output_refs:
        product_processing_result = process_product_outputs(settings=settings, product_output_refs=product_output_refs)
    entity_processing_result = None
    if not args.dry_run and entity_output_refs:
        entity_processing_result = process_entity_outputs(settings=settings, entity_output_refs=entity_output_refs)
    score_workflow_result = None
    if not args.dry_run and payload.get("saved_outputs"):
        score_workflow_result = trigger_score_workflows(settings=settings, saved_outputs=payload.get("saved_outputs") or [])

    payload["product_output_processing"] = asdict(product_processing_result) if product_processing_result else None
    payload["entity_output_processing"] = asdict(entity_processing_result) if entity_processing_result else None
    payload["score_workflow_trigger"] = asdict(score_workflow_result) if score_workflow_result else None
    payload["product_outputs_summary"] = {
        "output_ref_count": len(product_output_refs),
        "product_count": sum(len(ref.get("products") or []) for ref in product_output_refs if isinstance(ref, dict)),
    }
    payload["entity_outputs_summary"] = {
        "output_ref_count": len(entity_output_refs),
        "entity_count": sum(len(ref.get("entities") or []) for ref in entity_output_refs if isinstance(ref, dict)),
    }
    LOGGER.info("Extraction run finished: %s", payload)
    if result.failed_count:
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run BrandSight prompts through ChatGPT and save outputs.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--batch-id", help="BrandSight batch UUID to load prompts from.")
    source.add_argument("--prompts-file", type=Path, help="Local prompts JSON file, e.g. chromeApp/extension-shared/prompts.json.")
    parser.add_argument("--login-only", action="store_true", help="Open ChatGPT and wait for login using the persistent Chrome profile.")
    parser.add_argument("--brand-id", help="Brand UUID override for local prompts.")
    parser.add_argument("--limit", type=int, help="Maximum prompts to run.")
    parser.add_argument("--skip", type=int, default=0, help="Number of loaded prompts to skip.")
    parser.add_argument("--dry-run", action="store_true", help="Load prompts and print a preview without opening ChatGPT.")
    parser.add_argument("--force-rerun", action="store_true", help="Run prompts even when an output already exists for the same batch, brand, and prompt.")
    parser.add_argument(
        "--llm-model-filter",
        default="gpt",
        help="Only treat prompt outputs whose llm_model contains this value as completed. Defaults to 'gpt'. Use an empty string to match any model.",
    )
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None, help="Override CHATGPT_HEADLESS.")
    parser.add_argument("--chrome-user-data-dir", help="Chrome profile directory to reuse for ChatGPT login.")
    parser.add_argument(
        "--sources-panel-pause-seconds",
        type=int,
        default=0,
        help="Debug pause after opening the ChatGPT Sources panel. Defaults to 0; set e.g. 180 to inspect/copy DOM.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def quiet_third_party_http_logs() -> None:
    for logger_name in [
        "hpack",
        "httpcore",
        "httpx",
        "postgrest",
        "realtime",
        "supabase",
        "urllib3",
    ]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
