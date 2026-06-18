#!/usr/bin/env python3
"""
One-time setup: open Chrome with the Perplexity profile directory and wait for
you to log in manually. Once logged in, press Enter here to save and exit.

Usage:
    python scripts/setup_perplexity_profile.py
    python scripts/setup_perplexity_profile.py --profile /path/to/.perplexity-profile
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automated_extraction.perplexity_runner import PerplexityRunner, CHAT_INPUT_SELECTORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s", datefmt="%H:%M:%S")
LOGGER = logging.getLogger("setup_perplexity_profile")

DEFAULT_PROFILE = str(Path(__file__).resolve().parents[1] / ".perplexity-profile")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up a logged-in Perplexity Chrome profile.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"Profile directory (default: {DEFAULT_PROFILE})")
    parser.add_argument("--url", default="https://www.perplexity.ai", help="Perplexity URL")
    args = parser.parse_args()

    LOGGER.info("Opening Chrome with profile: %s", args.profile)

    runner = PerplexityRunner(args.url, headless=False, chrome_user_data_dir=args.profile, login_wait_seconds=600)

    with runner:
        runner.driver.get(args.url)
        print("\n" + "=" * 60)
        print("Chrome is open. Please log in to perplexity.ai in the browser.")
        print("Once logged in and the search input is visible, press Enter here.")
        print("=" * 60 + "\n")
        input("Press Enter when logged in > ")

        el = runner.find_first(CHAT_INPUT_SELECTORS)
        if el:
            LOGGER.info("✓ Chat input found! Profile is ready at: %s", args.profile)
        else:
            LOGGER.warning("Could not detect chat input — check selectors or try again.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
