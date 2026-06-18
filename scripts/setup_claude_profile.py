#!/usr/bin/env python3
"""
One-time setup: open Chrome with the Claude profile directory and wait for you
to log in manually. Once logged in and the chat input is visible, press Enter
here to save the session and exit.

Usage:
    python scripts/setup_claude_profile.py
    python scripts/setup_claude_profile.py --profile /path/to/.claude-profile
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automated_extraction.claude_runner import ClaudeRunner, CHAT_INPUT_SELECTORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s", datefmt="%H:%M:%S")
LOGGER = logging.getLogger("setup_claude_profile")

DEFAULT_PROFILE = str(Path(__file__).resolve().parents[1] / ".claude-profile")


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up a logged-in Claude Chrome profile.")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"Profile directory (default: {DEFAULT_PROFILE})")
    parser.add_argument("--url", default="https://claude.ai", help="Claude URL")
    args = parser.parse_args()

    LOGGER.info("Opening Chrome with profile: %s", args.profile)
    LOGGER.info("Profile dir will be created if it does not exist.")

    runner = ClaudeRunner(args.url, headless=False, chrome_user_data_dir=args.profile, login_wait_seconds=600)
    runner.start.__func__  # just to confirm it's accessible

    # Start the driver without calling wait_for_login
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.clipboard": 1,
        "profile.content_settings.exceptions.clipboard": {
            "https://claude.ai:443,*": {"setting": 1},
        },
    })

    driver = runner.create_driver(options)
    runner.driver = driver
    runner._persistent_chrome = False
    runner._set_window_size()
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    driver.get(args.url)

    print("\n" + "=" * 60)
    print("Chrome is open. Please log in to claude.ai in the browser.")
    print("Once you can see the chat input, press Enter here to save and exit.")
    print("=" * 60 + "\n")
    input("Press Enter when logged in and chat is visible > ")

    # Verify the input is findable
    el = runner.find_first(CHAT_INPUT_SELECTORS)
    if el:
        LOGGER.info("✓ Chat input found! Profile is ready at: %s", args.profile)
    else:
        LOGGER.warning("Could not detect chat input — check selectors or try again.")

    driver.quit()
    runner.driver = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
