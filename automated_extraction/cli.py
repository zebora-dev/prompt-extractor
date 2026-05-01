from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api_client import ApiClient
from .config import Settings


LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

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

    api = ApiClient(settings.api_base_url, settings.anon_key)

    prompts, batch_id, brand_id = load_prompt_work(args, api)
    prompts = prompts[args.skip :]
    if args.limit:
        prompts = prompts[: args.limit]

    LOGGER.info("Loaded %s prompt(s). batch_id=%s brand_id=%s", len(prompts), batch_id or "local", brand_id or "mixed")
    if args.dry_run:
        for prompt in prompts[:5]:
            LOGGER.info("Dry run prompt: id=%s brand_id=%s text=%r", prompt.get("id"), prompt.get("brand_id"), prompt_text(prompt)[:120])
        return 0

    from .chatgpt_runner import ChatGPTRunner

    with ChatGPTRunner(
        settings.chatgpt_url,
        headless=args.headless if args.headless is not None else settings.headless,
        chrome_user_data_dir=args.chrome_user_data_dir or settings.chrome_user_data_dir,
        login_wait_seconds=settings.login_wait_seconds,
        response_timeout_seconds=settings.response_timeout_seconds,
        sources_panel_pause_seconds=args.sources_panel_pause_seconds,
    ) as runner:
        for index, prompt in enumerate(prompts, start=1):
            prompt_id = str(prompt.get("id") or "")
            prompt_brand_id = str(prompt.get("brand_id") or brand_id or "")
            if not prompt_id or not prompt_brand_id:
                LOGGER.warning("Skipping prompt missing id or brand_id: %s", prompt)
                continue

            if batch_id and api.prompt_output_exists(prompt_id, prompt_brand_id, batch_id):
                LOGGER.info("[%s/%s] Skipping existing output for prompt %s", index, len(prompts), prompt_id)
                continue

            text = prompt_text(prompt)
            LOGGER.info("[%s/%s] Running prompt %s", index, len(prompts), prompt_id)
            capture = runner.run_prompt(text)
            output = build_prompt_output(
                prompt,
                capture.response,
                capture.capture_method,
                capture.raw_html,
                capture.raw_html_capture_method,
                capture.llm_model,
                capture.url,
                batch_id,
                capture.sources,
                capture.source_capture_method,
            )
            LOGGER.info(
                "[%s/%s] Capture summary for prompt %s: markdown_length=%s raw_html_length=%s llm_model=%s source_count=%s source_method=%s",
                index,
                len(prompts),
                prompt_id,
                len(capture.response or ""),
                len(capture.raw_html or ""),
                capture.llm_model,
                len(capture.sources or []),
                capture.source_capture_method,
            )
            saved = api.save_prompt_output(output)
            LOGGER.info("[%s/%s] Saved prompt %s: %s", index, len(prompts), prompt_id, saved or "ok")

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


def load_prompt_work(args: argparse.Namespace, api: ApiClient) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if args.prompts_file:
        with args.prompts_file.open("r", encoding="utf-8") as handle:
            prompts = json.load(handle)
        if not isinstance(prompts, list):
            raise RuntimeError(f"Expected a JSON array in {args.prompts_file}")
        brand_id = args.brand_id or first_brand_id(prompts)
        prompts = [with_brand_id(prompt, brand_id) for prompt in prompts]
        return prompts, None, brand_id

    batch = api.get_batch(args.batch_id)
    brand_id = args.brand_id or batch.get("brand_id")
    if not brand_id:
        raise RuntimeError(f"Batch {args.batch_id} does not include brand_id")
    return api.get_prompts(args.batch_id, str(brand_id)), args.batch_id, str(brand_id)


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


def build_prompt_output(
    prompt: dict[str, Any],
    response: str,
    capture_method: str,
    raw_html: str,
    raw_html_capture_method: str,
    llm_model: str,
    url: str,
    batch_id: str | None,
    sources: list[dict[str, Any]] | None = None,
    source_capture_method: str = "none",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "prompt_id": prompt.get("id"),
        "brand_id": prompt.get("brand_id"),
        "response": response,
        "markdown": response,
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
            "brand_description": (prompt.get("brand") or {}).get("description") if isinstance(prompt.get("brand"), dict) else None,
            "approved": prompt.get("approved"),
            "active": prompt.get("active"),
            "created_at": prompt.get("created_at"),
            "original_metadata": {
                "main_response_capture_method": capture_method,
                "copy_validation_status": "validated" if capture_method.startswith("copy_button") else "fallback",
                "raw_html_capture_method": raw_html_capture_method,
                "raw_html_length": len(raw_html or ""),
                "url": url,
                "source_count": len(sources or []),
                "source_capture_method": source_capture_method,
            },
            "site_used": "OpenAI",
            "timestamp": now,
        },
        "version_info": {
            "app_type": "automated_extraction",
            "app_version": "1.0.0",
            "extension_version": None,
            "prompt_source": "batch" if batch_id else "local",
        },
    }
