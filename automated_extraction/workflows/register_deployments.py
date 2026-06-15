from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from prefect.types.entrypoint import EntrypointType

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

WORK_POOL_NAME = os.getenv("PREFECT_WORK_POOL", "prompt-extraction-pool")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# On the Fly.io worker the app lives at /app; PREFECT_WORKING_DIR is set in
# the machine env. When registering from a local machine against the remote
# API, pass PREFECT_WORKING_DIR=/app explicitly so the worker finds the code.
WORK_DIR = os.getenv("PREFECT_WORKING_DIR") or str(PROJECT_ROOT)

# Supported regions. Each maps to a deployment name suffix and a region tag.
REGIONS = {
    "us": {"suffix": "", "tag": "region:us"},
    "uk": {"suffix": "-uk", "tag": "region:uk"},
}


def get_flows():
    from automated_extraction.workflows.dispatcher import dispatch_extraction_flow
    from automated_extraction.workflows.flows import (
        google_ai_mode_extraction_batch_flow,
        google_ai_mode_extraction_flow,
        google_ai_overview_extraction_batch_flow,
        google_ai_overview_extraction_flow,
        prompt_extraction_batch_flow,
        prompt_extraction_flow,
        prompt_output_processing_flow,
    )
    from automated_extraction.workflows.scaler_flows import (
        scale_workers_down_flow,
        scale_workers_flow,
    )

    return {
        "chatgpt-extraction-batch": {
            "flow": prompt_extraction_batch_flow,
            "tags": ["chatgpt", "extraction", "browser", "batch"],
            "description": "Sequentially run chatgpt-extraction in chunks until remaining prompts for a batch are covered.",
            "parameters": {
                "batch_id": None,
                "model_filter": "gpt",
                "limit": 5,
                "skip": 0,
                "auto_login": False,
                "login_email": None,
                "capture_products": True,
                "capture_entities": True,
                "trigger_scoring": True,
                "delay_seconds": 120,
                "startup_delay_seconds": 0,
            },
        },
        "chatgpt-extraction": {
            "flow": prompt_extraction_flow,
            "tags": ["chatgpt", "extraction", "browser"],
            "description": "Run BrandSight prompts through ChatGPT and save markdown, raw HTML, and sources.",
            "parameters": {
                "batch_id": None,
                "prompts_file": None,
                "brand_id": None,
                "limit": None,
                "skip": 0,
                "dry_run": False,
                "headless": None,
                "chrome_user_data_dir": None,
                "sources_panel_pause_seconds": 0,
                "force_rerun": False,
                "llm_model_filter": "gpt",
                "auto_login": False,
                "login_email": None,
                "capture_products": True,
                "capture_entities": True,
                "trigger_scoring": True,
            },
        },
        "prompt-output-processing": {
            "flow": prompt_output_processing_flow,
            "tags": ["prompt-output", "post-process", "markdown"],
            "description": "Re-process saved prompt outputs from raw HTML without running ChatGPT extraction.",
            "parameters": {
                "output_id": None,
                "batch_id": None,
                "brand_id": None,
                "prompt_id": None,
                "limit": 50,
            },
        },
        "dispatch-extraction": {
            "flow": dispatch_extraction_flow,
            "tags": ["dispatcher", "extraction", "orchestration"],
            "description": (
                "Automatically split a batch across N workers. "
                "Counts remaining prompts, divides into equal chunks, and submits "
                "one batch flow run per worker — then exits. "
                "Supports google-ai-overview, google-ai-mode, and chatgpt. "
                "Set auto_scale=True to automatically scale Fly.io machines up "
                "to worker_count before dispatching (requires FLY_API_TOKEN secret)."
            ),
            "parameters": {
                "batch_id": None,
                "extraction_type": "google-ai-overview",
                "worker_count": 4,
                "region": "uk",
                "limit": 5,
                "max_prompts": None,
                "delay_seconds": 60,
                "use_proxy": False,
                "country": None,
                "language": None,
                "auto_login": False,
                "login_email": None,
                "capture_products": True,
                "capture_entities": True,
                "trigger_scoring": True,
                "auto_scale": False,
                "scale_wait_seconds": 30,
                "stagger_seconds": 15,
            },
        },
        "scale-workers": {
            "flow": scale_workers_flow,
            "tags": ["scaling", "infrastructure", "fly"],
            "description": (
                "Scale Fly.io worker machines up to target_count. "
                "Starts stopped original machines first, then clones new ones. "
                "Updates the Prefect work-pool concurrency limit to match. "
                "Requires FLY_API_TOKEN secret to be set on the app."
            ),
            "parameters": {
                "target_count": 4,
                "region": "uk",
                "work_pool": None,
                "wait_for_workers_seconds": 30,
            },
        },
        "scale-workers-down": {
            "flow": scale_workers_down_flow,
            "tags": ["scaling", "infrastructure", "fly"],
            "description": (
                "Scale Fly.io worker machines back down. "
                "Destroys all cloned machines and stops original machines above keep_count. "
                "Resets the Prefect work-pool concurrency limit to keep_count."
            ),
            "parameters": {
                "region": "uk",
                "keep_count": 1,
                "work_pool": None,
            },
        },
        "google-ai-mode-extraction-batch": {
            "flow": google_ai_mode_extraction_batch_flow,
            "tags": ["google", "ai-mode", "extraction", "browser", "batch"],
            "description": "Sequentially run google-ai-mode-extraction in chunks until all remaining prompts in a batch are covered.",
            "parameters": {
                "batch_id": None,
                "model_filter": "google-ai-mode",
                "limit": 5,
                "skip": 0,
                "delay_seconds": 60,
                "country": None,
                "language": None,
                "use_proxy": False,
                "trigger_scoring": True,
                "startup_delay_seconds": 0,
            },
        },
        "google-ai-overview-extraction-batch": {
            "flow": google_ai_overview_extraction_batch_flow,
            "tags": ["google", "ai-overview", "extraction", "browser", "batch"],
            "description": "Sequentially run google-ai-overview-extraction in chunks until all remaining prompts in a batch are covered.",
            "parameters": {
                "batch_id": None,
                "model_filter": "google-ai-overview",
                "limit": 5,
                "skip": 0,
                "delay_seconds": 60,
                "country": None,
                "language": None,
                "use_proxy": False,
                "trigger_scoring": True,
                "startup_delay_seconds": 0,
            },
        },
        "google-ai-mode-extraction": {
            "flow": google_ai_mode_extraction_flow,
            "tags": ["google", "ai-mode", "extraction", "browser"],
            "description": "Run BrandSight prompts through Google Search and save AI Mode markdown, raw HTML, and citations.",
            "parameters": {
                "batch_id": None,
                "prompts_file": None,
                "brand_id": None,
                "limit": None,
                "skip": 0,
                "dry_run": False,
                "headless": None,
                "chrome_user_data_dir": None,
                "force_rerun": False,
                "llm_model_filter": "google-ai-mode",
                "country": None,
                "language": None,
                "use_proxy": False,
                "trigger_scoring": True,
            },
        },
        "google-ai-overview-extraction": {
            "flow": google_ai_overview_extraction_flow,
            "tags": ["google", "ai-overview", "extraction", "browser"],
            "description": "Run BrandSight prompts through Google Search and save organic AI Overview responses, sources, and citations.",
            "parameters": {
                "batch_id": None,
                "prompts_file": None,
                "brand_id": None,
                "limit": None,
                "skip": 0,
                "dry_run": False,
                "headless": None,
                "chrome_user_data_dir": None,
                "force_rerun": False,
                "llm_model_filter": "google-ai-overview",
                "country": None,
                "language": None,
                "use_proxy": False,
                "trigger_scoring": True,
            },
        },
    }


def _deployment_name(base_name: str, suffix: str) -> str:
    return f"{base_name}{suffix}"


def serve_deployments() -> None:
    from prefect import serve

    deployments = []
    for name, config in get_flows().items():
        deployments.append(
            config["flow"].to_deployment(
                name=name,
                tags=config["tags"],
                description=config["description"],
                parameters=config.get("parameters", {}),
            )
        )

    LOGGER.info("Serving %s deployment(s) locally.", len(deployments))
    LOGGER.info("Prefect UI: http://localhost:4200")
    serve(*deployments)


async def deploy_with_local_storage(region: str = "us") -> None:
    region_config = REGIONS.get(region)
    if region_config is None:
        raise ValueError(f"Unknown region {region!r}. Choose from: {list(REGIONS)}")

    suffix = region_config["suffix"]
    region_tag = region_config["tag"]

    for base_name, config in get_flows().items():
        deployment_name = _deployment_name(base_name, suffix)
        tags = [*config["tags"], region_tag]
        try:
            await config["flow"].deploy(
                name=deployment_name,
                work_pool_name=WORK_POOL_NAME,
                job_variables={
                    "working_dir": WORK_DIR,
                    "env": {
                        "PYTHONPATH": WORK_DIR,
                    },
                },
                tags=tags,
                description=config["description"],
                parameters=config.get("parameters", {}),
                build=False,
                push=False,
                entrypoint_type=EntrypointType.MODULE_PATH,
                ignore_warnings=True,
            )
            LOGGER.info(
                "Deployed %s (region=%s) to work pool %s",
                deployment_name,
                region,
                WORK_POOL_NAME,
            )
        except Exception as exc:
            LOGGER.error("Failed to deploy %s: %s", deployment_name, exc)
            raise


def create_work_pool() -> bool:
    import httpx

    prefect_api_url = os.getenv("PREFECT_API_URL", "http://localhost:4200/api")
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{prefect_api_url}/work_pools/{WORK_POOL_NAME}")
            if response.status_code == 200:
                LOGGER.info("Work pool %s already exists", WORK_POOL_NAME)
                return True

            response = client.post(
                f"{prefect_api_url}/work_pools/",
                json={
                    "name": WORK_POOL_NAME,
                    "type": "process",
                    "description": "Work pool for prompt extraction workflows",
                    "is_paused": False,
                },
            )
            if response.status_code in {200, 201}:
                LOGGER.info("Created work pool %s", WORK_POOL_NAME)
                return True
            LOGGER.error("Failed to create work pool: %s", response.text)
            return False
    except Exception as exc:
        LOGGER.error("Could not connect to Prefect API: %s", exc)
        LOGGER.info("Start Prefect first: prefect server start")
        return False


def list_deployments(region: str | None = None) -> None:
    regions_to_show = [region] if region else list(REGIONS)
    print("\nAvailable Prompt Extraction Workflows:")
    print("=" * 60)
    for base_name, config in get_flows().items():
        print(f"\n{base_name}")
        print(f"  Description: {config['description']}")
        print(f"  Tags: {', '.join(config['tags'])}")
        print(f"  Default params: {config.get('parameters', {})}")
        print("  Deployments:")
        for r in regions_to_show:
            rc = REGIONS[r]
            dep_name = _deployment_name(base_name, rc["suffix"])
            print(f"    [{r}] {dep_name}  ({rc['tag']})")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Prefect prompt extraction workflow deployments",
    )
    parser.add_argument("--serve", action="store_true", help="Serve deployments locally.")
    parser.add_argument("--deploy-local", action="store_true", help="Deploy flows for local process workers.")
    parser.add_argument("--create-pool", action="store_true", help="Create the process work pool.")
    parser.add_argument("--list", action="store_true", help="List available workflows.")
    parser.add_argument(
        "--region",
        choices=list(REGIONS),
        default="us",
        help=(
            "Region to deploy for. Controls the deployment name suffix and region tag. "
            "Must match the PREFECT_WORK_POOL set for that region's worker. "
            "Default: us"
        ),
    )
    args = parser.parse_args()

    if not any([args.serve, args.deploy_local, args.create_pool, args.list]):
        parser.print_help()
        print("\nFor local development, use: --serve")
        print("To deploy for US workers: --deploy-local --region us")
        print("To deploy for UK workers: --deploy-local --region uk")
        sys.exit(0)

    if args.list:
        list_deployments(args.region if args.region else None)
        return

    if args.create_pool:
        if not create_work_pool():
            sys.exit(1)
        if not args.serve and not args.deploy_local:
            return

    if args.serve:
        serve_deployments()
    elif args.deploy_local:
        asyncio.run(deploy_with_local_storage(region=args.region))


if __name__ == "__main__":
    main()
