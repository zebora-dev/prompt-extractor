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


def get_flows():
    from automated_extraction.workflows.flows import (
        google_ai_mode_extraction_flow,
        google_ai_overview_extraction_flow,
        prompt_extraction_batch_flow,
        prompt_extraction_flow,
        prompt_output_processing_flow,
    )

    return {
        "prompt-extraction-batch": {
            "flow": prompt_extraction_batch_flow,
            "tags": ["chatgpt", "extraction", "browser", "batch"],
            "description": "Sequentially run prompt-extraction in chunks until remaining prompts for a batch are covered.",
            "parameters": {
                "batch_id": None,
                "model_filter": "gpt",
                "limit": 10,
                "skip": 0,
                "auto_login": False,
                "login_email": None,
                "capture_products": False,
                "capture_entities": False,
                "delay_seconds": 120,
            },
        },
        "prompt-extraction": {
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
                "capture_products": False,
                "capture_entities": False,
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
            },
        },
    }


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


async def deploy_with_local_storage() -> None:
    for name, config in get_flows().items():
        try:
            await config["flow"].deploy(
                name=name,
                work_pool_name=WORK_POOL_NAME,
                job_variables={
                    "working_dir": WORK_DIR,
                    "env": {
                        "PYTHONPATH": WORK_DIR,
                    },
                },
                tags=config["tags"],
                description=config["description"],
                parameters=config.get("parameters", {}),
                build=False,
                push=False,
                entrypoint_type=EntrypointType.MODULE_PATH,
                ignore_warnings=True,
            )
            LOGGER.info("Deployed %s to work pool %s", name, WORK_POOL_NAME)
        except Exception as exc:
            LOGGER.error("Failed to deploy %s: %s", name, exc)
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


def list_deployments() -> None:
    print("\nAvailable Prompt Extraction Workflows:")
    print("=" * 60)
    for name, config in get_flows().items():
        print(f"\n{name}")
        print(f"  Description: {config['description']}")
        print(f"  Tags: {', '.join(config['tags'])}")
        print(f"  Default params: {config.get('parameters', {})}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Prefect prompt extraction workflow deployments",
    )
    parser.add_argument("--serve", action="store_true", help="Serve deployments locally.")
    parser.add_argument("--deploy-local", action="store_true", help="Deploy flows for local process workers.")
    parser.add_argument("--create-pool", action="store_true", help="Create the process work pool.")
    parser.add_argument("--list", action="store_true", help="List available workflows.")
    args = parser.parse_args()

    if not any([args.serve, args.deploy_local, args.create_pool, args.list]):
        parser.print_help()
        print("\nFor local development, use: --serve")
        sys.exit(0)

    if args.list:
        list_deployments()
        return

    if args.create_pool:
        if not create_work_pool():
            sys.exit(1)
        if not args.serve and not args.deploy_local:
            return

    if args.serve:
        serve_deployments()
    elif args.deploy_local:
        asyncio.run(deploy_with_local_storage())


if __name__ == "__main__":
    main()
