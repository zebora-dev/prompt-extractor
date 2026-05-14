"""
Centralised Chrome driver factory for Google extraction runners.

Handles:
- undetected_chromedriver (primary) with optional selenium-wire for proxy auth
- selenium-stealth fingerprint patches (graceful no-op if not installed)
- CDP navigator.webdriver override injected before any page script runs
- Rotating user-agent strings
- Optional residential proxy via GOOGLE_PROXY_URL env var or explicit proxy_url
- Session warmup (homepage visit before first search)
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options

from .chatgpt_runner import detect_chrome_major_version, first_line

LOGGER = logging.getLogger(__name__)

# Chrome 135-137 on Windows/macOS — updated May 2026.
# Keep platform consistent with selenium-stealth's platform arg.
_CHROME_USER_AGENTS: list[tuple[str, str]] = [
    # (user_agent, stealth_platform)
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.100 Safari/537.36",
        "Win32",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "MacIntel",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
        "MacIntel",
    ),
]

# navigator.webdriver override injected at document creation — before any page JS runs.
_WEBDRIVER_OVERRIDE_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def build_google_driver(
    *,
    headless: bool = False,
    user_data_dir: str | None = None,
    proxy_url: str | None = None,
) -> Chrome:
    """
    Build a Chrome driver configured for Google extraction with stealth patches.

    proxy_url: full proxy URL, e.g. ``http://user:pass@host:port``.
               When provided, uses selenium-wire to authenticate the proxy.
               Falls back to plain undetected_chromedriver if selenium-wire
               is unavailable.
    """
    user_agent, stealth_platform = random.choice(_CHROME_USER_AGENTS)
    LOGGER.info(
        "Building Google Chrome driver. proxy=%s headless=%s ua=%s",
        "yes" if proxy_url else "no",
        headless,
        user_agent[:60],
    )

    driver = _create_driver(
        headless=headless,
        user_data_dir=user_data_dir,
        proxy_url=proxy_url,
        user_agent=user_agent,
    )
    _apply_stealth(driver, user_agent=user_agent, platform=stealth_platform)
    return driver


def warmup_google_session(driver: Chrome, warmup_url: str = "https://www.google.com") -> None:
    """Visit the Google homepage to establish cookies before the first search."""
    try:
        LOGGER.info("Warming up Google session via %s", warmup_url)
        driver.get(warmup_url)
        time.sleep(random.uniform(2.0, 3.5))
    except Exception as exc:
        LOGGER.warning("Session warmup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _create_driver(
    *,
    headless: bool,
    user_data_dir: str | None,
    proxy_url: str | None,
    user_agent: str,
) -> Chrome:
    common_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        f"--window-size={random.randint(1880, 1920)},{random.randint(1060, 1080)}",
        f"--user-agent={user_agent}",
    ]
    if headless:
        common_args.append("--headless=new")

    # Try with proxy (selenium-wire + undetected_chromedriver)
    if proxy_url:
        driver = _try_seleniumwire_uc(
            common_args=common_args,
            user_data_dir=user_data_dir,
            proxy_url=proxy_url,
        )
        if driver is not None:
            return driver
        LOGGER.warning("selenium-wire unavailable — falling back to driver without proxy.")

    # Try without proxy (undetected_chromedriver)
    driver = _try_undetected_uc(
        common_args=common_args,
        user_data_dir=user_data_dir,
    )
    if driver is not None:
        return driver

    # Final fallback: plain Selenium Chrome
    LOGGER.warning("Falling back to standard webdriver.Chrome (no stealth).")
    options = Options()
    for arg in common_args:
        options.add_argument(arg)
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    return webdriver.Chrome(options=options)


def _try_seleniumwire_uc(
    *,
    common_args: list[str],
    user_data_dir: str | None,
    proxy_url: str,
) -> Chrome | None:
    try:
        import seleniumwire.undetected_chromedriver as sw_uc

        options = sw_uc.ChromeOptions()
        for arg in common_args:
            options.add_argument(arg)

        sw_options: dict[str, Any] = {
            "proxy": {
                "http": proxy_url,
                "https": proxy_url,
                "no_proxy": "localhost,127.0.0.1",
            }
        }

        kwargs: dict[str, Any] = {"seleniumwire_options": sw_options}
        if user_data_dir:
            kwargs["user_data_dir"] = user_data_dir
        chrome_major = detect_chrome_major_version()
        if chrome_major:
            kwargs["version_main"] = chrome_major

        LOGGER.info("Using selenium-wire + undetected_chromedriver with proxy.")
        return sw_uc.Chrome(options=options, **kwargs)
    except (ImportError, ModuleNotFoundError) as exc:
        LOGGER.debug("selenium-wire not available: %s", exc)
        return None
    except SessionNotCreatedException as exc:
        LOGGER.warning("selenium-wire session failed (%s).", first_line(str(exc)))
        return None


def _try_undetected_uc(
    *,
    common_args: list[str],
    user_data_dir: str | None,
) -> Chrome | None:
    try:
        uc = _import_uc()
        options = uc.ChromeOptions()
        for arg in common_args:
            options.add_argument(arg)

        kwargs: dict[str, Any] = {}
        if user_data_dir:
            kwargs["user_data_dir"] = user_data_dir
        chrome_major = detect_chrome_major_version()
        if chrome_major:
            kwargs["version_main"] = chrome_major

        LOGGER.info("Using undetected_chromedriver (no proxy).")
        return uc.Chrome(options=options, **kwargs)
    except (ImportError, ModuleNotFoundError) as exc:
        LOGGER.warning("undetected_chromedriver not available: %s", exc)
        return None
    except SessionNotCreatedException as exc:
        LOGGER.warning("undetected_chromedriver session failed (%s).", first_line(str(exc)))
        return None


def _apply_stealth(driver: Chrome, *, user_agent: str, platform: str) -> None:
    # Inject navigator.webdriver override before any page script runs.
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _WEBDRIVER_OVERRIDE_SCRIPT},
        )
    except Exception as exc:
        LOGGER.debug("CDP navigator.webdriver override failed: %s — falling back to execute_script", exc)
        try:
            driver.execute_script(_WEBDRIVER_OVERRIDE_SCRIPT)
        except Exception:
            pass

    # selenium-stealth: patches plugins, languages, WebGL, window.chrome, etc.
    try:
        from selenium_stealth import stealth  # type: ignore[import-untyped]

        stealth(
            driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform=platform,
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
        LOGGER.debug("selenium-stealth patches applied.")
    except ImportError:
        LOGGER.debug("selenium-stealth not installed — skipping fingerprint patches.")
    except Exception as exc:
        LOGGER.debug("selenium-stealth failed (non-fatal): %s", exc)


def _import_uc():
    """Import undetected_chromedriver, working around setuptools distutils shim."""
    try:
        import undetected_chromedriver as uc

        return uc
    except ModuleNotFoundError as error:
        if error.name != "distutils":
            raise
        try:
            import setuptools._distutils as distutils_module
            import setuptools._distutils.version as distutils_version_module
        except ModuleNotFoundError:
            raise error
        sys.modules.setdefault("distutils", distutils_module)
        sys.modules.setdefault("distutils.version", distutils_version_module)
        import undetected_chromedriver as uc

        return uc


def resolve_proxy_url(use_proxy: bool) -> str | None:
    """Return the proxy URL to use, or None if proxying is disabled."""
    if not use_proxy:
        return None
    url = os.getenv("GOOGLE_PROXY_URL", "").strip()
    if not url:
        LOGGER.warning("use_proxy=True but GOOGLE_PROXY_URL env var is not set — running without proxy.")
        return None
    return url
