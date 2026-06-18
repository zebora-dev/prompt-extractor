#!/usr/bin/env python3
"""
Stage 1 local test script for PerplexityRunner.

Usage:
    python scripts/test_perplexity_local.py
    python scripts/test_perplexity_local.py --prompt "What are the best CRMs for small teams?"
    python scripts/test_perplexity_local.py --profile /path/to/chrome/profile

Env vars (from .env or shell):
    PERPLEXITY_CHROME_USER_DATA_DIR  — path to a Chrome profile logged in to perplexity.ai
    PERPLEXITY_URL                   — defaults to https://www.perplexity.ai
    CHATGPT_HEADLESS                 — set to "true" to run headless
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automated_extraction.config import load_dotenv_if_available
from automated_extraction.perplexity_runner import PerplexityRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("test_perplexity_local")


def main() -> int:
    load_dotenv_if_available()

    parser = argparse.ArgumentParser(description="Test PerplexityRunner locally.")
    parser.add_argument(
        "--prompt",
        default="What are the top 5 project management tools in 2024?",
        help="Prompt text to send to Perplexity.",
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("PERPLEXITY_CHROME_USER_DATA_DIR") or str(Path(__file__).resolve().parents[1] / ".perplexity-profile"),
        help="Path to a Chrome user data directory already logged in to perplexity.ai.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("PERPLEXITY_URL", "https://www.perplexity.ai"),
        help="Perplexity URL (default: https://www.perplexity.ai)",
    )
    parser.add_argument("--headless", action="store_true", help="Run headless.")
    args = parser.parse_args()

    LOGGER.info("Starting PerplexityRunner. profile=%s url=%s headless=%s", args.profile, args.url, args.headless)
    LOGGER.info("Prompt: %r", args.prompt)

    runner = PerplexityRunner(
        args.url,
        headless=args.headless,
        chrome_user_data_dir=args.profile,
        login_wait_seconds=120,
        response_timeout_seconds=180,
    )

    with runner:
        LOGGER.info("Browser started. Running prompt …")
        capture = runner.run_prompt(args.prompt)

    print("\n" + "=" * 60)
    print(f"MODEL:   {capture.llm_model}")
    print(f"URL:     {capture.url}")
    print(f"CAPTURE: {capture.capture_method} / markdown={capture.markdown_capture_method}")
    print(f"HTML:    {len(capture.raw_html)} chars ({capture.raw_html_capture_method})")
    print(f"SOURCES: {len(capture.sources)} ({capture.source_capture_method})")
    print("=" * 60)
    print("\n--- RESPONSE (first 800 chars) ---")
    print(capture.response[:800])
    if capture.markdown:
        print("\n--- MARKDOWN (first 500 chars) ---")
        print(capture.markdown[:500])
    if capture.sources:
        print("\n--- SOURCES ---")
        for s in capture.sources[:5]:
            print(f"  [{s.get('index', '?')}] {s['url']}  ({s.get('title', '')})")
    print("\n✓ Test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
