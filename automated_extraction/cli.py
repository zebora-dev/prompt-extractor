from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .entity_output_processor import process_entity_outputs
from .extraction import run_extraction_job, run_google_ai_mode_extraction_job, run_google_ai_overview_extraction_job
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

    if args.llm_model_filter is None:
        if args.provider == "google-ai-mode":
            args.llm_model_filter = "google-ai-mode"
        elif args.provider == "google-ai-overview":
            args.llm_model_filter = "google-ai-overview"
        else:
            args.llm_model_filter = "gpt"

    auto_login_override = args.auto_login
    is_google_provider = args.provider in {"google-ai-mode", "google-ai-overview"}
    settings = Settings.from_env(
        require_api_key=not args.login_only,
        # When --auto-login is explicitly set, enforce credentials even for
        # --login-only so misconfiguration fails fast. Otherwise let the env
        # default decide. Google providers don't use ChatGPT credentials.
        require_auto_login_credentials=False
        if is_google_provider
        else (auto_login_override is True) or (not args.login_only),
    )
    auto_login = auto_login_override if auto_login_override is not None else settings.auto_login
    login_email = args.login_email or settings.login_email

    if args.capture_profile is not None:
        return _run_capture_profile(args, settings)

    if args.login_only:
        if is_google_provider:
            LOGGER.info(
                "--login-only is ChatGPT-specific. For Google providers, run Chrome once with "
                "--provider %s or set GOOGLE_CHROME_USER_DATA_DIR to a logged-in profile.",
                args.provider,
            )
            return 0
        from .chatgpt_runner import ChatGPTRunner

        profile_dir = args.chrome_user_data_dir or settings.chrome_user_data_dir
        LOGGER.info(
            "Opening ChatGPT login session with profile: %s (auto_login=%s, email=%s)",
            profile_dir,
            auto_login,
            login_email or "<unset>",
        )
        with ChatGPTRunner(
            settings.chatgpt_url,
            headless=False,
            chrome_user_data_dir=profile_dir,
            login_wait_seconds=settings.login_wait_seconds,
            response_timeout_seconds=settings.response_timeout_seconds,
            sources_panel_pause_seconds=args.sources_panel_pause_seconds,
            auto_login=auto_login,
            accounts=settings.accounts,
            login_email=login_email,
        ):
            LOGGER.info("ChatGPT login is ready and stored in the profile.")
        return 0

    if not args.batch_id and not args.prompts_file:
        parser.error("one of --batch-id or --prompts-file is required unless --login-only is used")

    if args.provider == "google-ai-overview":
        result = run_google_ai_overview_extraction_job(
            settings=settings,
            batch_id=args.batch_id,
            prompts_file=args.prompts_file,
            brand_id=args.brand_id,
            limit=args.limit,
            skip=args.skip,
            dry_run=args.dry_run,
            headless=args.headless if args.headless is not None else settings.headless,
            chrome_user_data_dir=args.chrome_user_data_dir,
            force_rerun=args.force_rerun,
            llm_model_filter=args.llm_model_filter,
            country=args.google_country,
            language=args.google_language,
        )
    elif args.provider == "google-ai-mode":
        result = run_google_ai_mode_extraction_job(
            settings=settings,
            batch_id=args.batch_id,
            prompts_file=args.prompts_file,
            brand_id=args.brand_id,
            limit=args.limit,
            skip=args.skip,
            dry_run=args.dry_run,
            headless=args.headless if args.headless is not None else settings.headless,
            chrome_user_data_dir=args.chrome_user_data_dir,
            force_rerun=args.force_rerun,
            llm_model_filter=args.llm_model_filter,
            country=args.google_country,
            language=args.google_language,
        )
    else:
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
            auto_login=auto_login,
            login_email=login_email,
            capture_products=args.capture_products,
            capture_entities=args.capture_entities,
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
        score_workflow_result = trigger_score_workflows(
            settings=settings, saved_outputs=payload.get("saved_outputs") or []
        )

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
    parser.add_argument(
        "--provider",
        choices=["chatgpt", "google-ai-mode", "google-ai-overview"],
        default="chatgpt",
        help="Extraction provider to run. Defaults to chatgpt.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--batch-id", help="BrandSight batch UUID to load prompts from.")
    source.add_argument(
        "--prompts-file", type=Path, help="Local prompts JSON file, e.g. chromeApp/extension-shared/prompts.json."
    )
    parser.add_argument(
        "--login-only", action="store_true", help="Open ChatGPT and wait for login using the persistent Chrome profile."
    )
    parser.add_argument("--brand-id", help="Brand UUID override for local prompts.")
    parser.add_argument("--limit", type=int, help="Maximum prompts to run.")
    parser.add_argument("--skip", type=int, default=0, help="Number of loaded prompts to skip.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Load prompts and print a preview without opening ChatGPT."
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Run prompts even when an output already exists for the same batch, brand, and prompt.",
    )
    parser.add_argument(
        "--llm-model-filter",
        default=None,
        help=(
            "Only treat prompt outputs whose llm_model contains this value as completed. Defaults to "
            "'gpt' for ChatGPT, 'google-ai-mode' for Google AI Mode, 'google-ai-overview' for Google AI Overview. "
            "Use an empty string to match any model."
        ),
    )
    parser.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=None, help="Override CHATGPT_HEADLESS."
    )
    parser.add_argument("--chrome-user-data-dir", help="Chrome profile directory to reuse for ChatGPT login.")
    parser.add_argument(
        "--auto-login",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override CHATGPT_AUTO_LOGIN. When true, runs the BasicLogin/GoogleLogin flow using CHATGPT_ACCOUNTS_B64.",
    )
    parser.add_argument(
        "--login-email",
        help="Override CHATGPT_LOGIN_EMAIL. Selects which account from CHATGPT_ACCOUNTS_B64 to use.",
    )
    parser.add_argument(
        "--sources-panel-pause-seconds",
        type=int,
        default=0,
        help="Debug pause after opening the ChatGPT Sources panel. Defaults to 0; set e.g. 180 to inspect/copy DOM.",
    )
    parser.add_argument("--google-country", help="Google Search country code override, e.g. US or GB.")
    parser.add_argument("--google-language", help="Google Search language code override, e.g. en.")
    parser.add_argument(
        "--capture-products",
        action="store_true",
        default=False,
        help="Enable product flyout capture after each response. Disabled by default.",
    )
    parser.add_argument(
        "--capture-entities",
        action="store_true",
        default=False,
        help="Enable entity flyout capture after each response. Disabled by default.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")

    # ── Profile snapshot capture ──────────────────────────────────────────────
    parser.add_argument(
        "--capture-profile",
        type=int,
        metavar="INDEX",
        default=None,
        help=(
            "Open Chrome at ChatGPT, wait for manual login, then upload the profile as "
            "profile_{INDEX}.tar.gz to Supabase Storage. Use --chrome-user-data-dir to "
            "specify a custom profile path (defaults to CHATGPT_CHROME_USER_DATA_DIR)."
        ),
    )

    return parser


def _run_capture_profile(args: argparse.Namespace, settings) -> int:
    """
    Open Chrome (always non-headless), navigate to ChatGPT, wait for the user
    to log in manually (including any 2FA), then snapshot and upload the profile.
    """
    from .chatgpt_runner import ChatGPTRunner
    from .profile_manager import upload_profile

    index = args.capture_profile
    profile_dir = args.chrome_user_data_dir or settings.chrome_user_data_dir

    LOGGER.info(
        "=== Profile Capture Mode ===\n"
        "  Profile index : %d\n"
        "  Profile dir   : %s\n"
        "  Target object : profile_%d.tar.gz\n\n"
        "Chrome will open at ChatGPT. Log in manually (complete any 2FA), then come back "
        "here and press Enter to snapshot and upload the profile.",
        index,
        profile_dir,
        index,
    )

    # Open Chrome and navigate to ChatGPT — user logs in manually.
    with ChatGPTRunner(
        settings.chatgpt_url,
        headless=False,
        chrome_user_data_dir=profile_dir,
        login_wait_seconds=settings.login_wait_seconds,
        response_timeout_seconds=settings.response_timeout_seconds,
        sources_panel_pause_seconds=0,
        auto_login=False,
        accounts={},
        login_email=None,
    ):
        input("\n✅ Once you are logged in to ChatGPT, press Enter to snapshot the profile … ")

    LOGGER.info("Chrome closed. Uploading profile %d from %s …", index, profile_dir)
    upload_profile(index, profile_dir)
    LOGGER.info("Profile %d captured and uploaded successfully.", index)
    return 0


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
