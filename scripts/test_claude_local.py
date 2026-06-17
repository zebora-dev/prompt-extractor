#!/usr/bin/env python3
"""
Stage 1 local test script for ClaudeRunner.

Usage:
    python scripts/test_claude_local.py
    python scripts/test_claude_local.py --prompt "What are the best CRMs for small teams?"
    python scripts/test_claude_local.py --profile /path/to/chrome/profile

Env vars (from .env or shell):
    CLAUDE_CHROME_USER_DATA_DIR  — path to a Chrome profile already logged in to claude.ai
    CLAUDE_URL                   — defaults to https://claude.ai
    CHATGPT_HEADLESS             — set to "true" to run headless (usually fails CF)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make sure the package is importable from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automated_extraction.config import load_dotenv_if_available
from automated_extraction.claude_runner import ClaudeRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("test_claude_local")


def main() -> int:
    load_dotenv_if_available()

    parser = argparse.ArgumentParser(description="Test ClaudeRunner locally.")
    parser.add_argument(
        "--prompt",
        default="What are the top 5 project management tools in 2024?",
        help="Prompt text to send to Claude.",
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("CLAUDE_CHROME_USER_DATA_DIR") or str(Path(__file__).resolve().parents[1] / ".claude-profile"),
        help="Path to a Chrome user data directory already logged in to claude.ai.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("CLAUDE_URL", "https://claude.ai"),
        help="Claude URL (default: https://claude.ai)",
    )
    parser.add_argument("--headless", action="store_true", help="Run headless (not recommended — Cloudflare will block).")
    args = parser.parse_args()

    LOGGER.info("Starting ClaudeRunner. profile=%s url=%s headless=%s", args.profile, args.url, args.headless)
    LOGGER.info("Prompt: %r", args.prompt)

    runner = ClaudeRunner(
        args.url,
        headless=args.headless,
        chrome_user_data_dir=args.profile,
        login_wait_seconds=120,
        response_timeout_seconds=120,
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
        for s in capture.sources:
            print(f"  [{s['index']}] {s['url']}  ({s.get('title', '')})")
    print("\n✓ Test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
