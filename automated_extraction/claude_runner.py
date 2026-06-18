from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pyperclip
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    NoSuchElementException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains, Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automated_extraction.notifications import notify_cloudflare_challenge, notify_cloudflare_cleared

LOGGER = logging.getLogger(__name__)

# Claude uses a ProseMirror contenteditable div, not a <textarea>.
CHAT_INPUT_SELECTORS = [
    "div[data-testid='chat-input']",
    "div.tiptap.ProseMirror",
    "div[contenteditable='true'].ProseMirror",
    "div[aria-label='Write your prompt to Claude']",
    "div[contenteditable='true'][data-placeholder]",
    "main div[contenteditable='true']",
    "div[contenteditable='true']",
]

SEND_BUTTON_SELECTORS = [
    "button[aria-label='Send message']",
    "button[aria-label='Send Message']",
    "button[aria-label*='Send']",
    "button[data-testid='send-button']",
    "button[type='submit']",
]

STOP_BUTTON_SELECTORS = [
    "button[aria-label='Stop response']",
    "button[aria-label='Stop Response']",
    "button[aria-label*='Stop']",
    "button[aria-label*='stop']",
    "button[data-testid='stop-button']",
]

DISMISS_BUTTON_TEXT = {
    "accept",
    "agree",
    "close",
    "continue",
    "dismiss",
    "got it",
    "not now",
    "ok",
    "okay",
    "skip",
}

NEW_CHAT_URL = "https://claude.ai/new"

# Selector for the assistant response container.
# We try multiple candidates and use the last visible match.
ASSISTANT_RESPONSE_SELECTORS = [
    "div.font-claude-response",
    "div.standard-markdown",
    "div[data-testid='ai-message']",
    "div.font-claude-message",
]

# Composite CSS selector for fast WebDriverWait checks.
CHAT_INPUT_SELECTOR = (
    "div[data-testid='chat-input'], "
    "div.tiptap.ProseMirror, "
    "div[contenteditable='true'].ProseMirror, "
    "div[aria-label='Write your prompt to Claude']"
)
STOP_BUTTON_SELECTOR = ", ".join(STOP_BUTTON_SELECTORS[:3])


@dataclass
class ClaudeCapture:
    response: str
    markdown: str
    capture_method: str
    markdown_capture_method: str
    raw_html: str
    raw_html_capture_method: str
    llm_model: str
    url: str
    sources: list[dict[str, Any]]
    source_capture_method: str
    products: list[dict[str, Any]]
    product_capture_method: str
    entities: list[dict[str, Any]]
    entity_capture_method: str


class ClaudeRunner:
    def __init__(
        self,
        claude_url: str,
        *,
        headless: bool = False,
        chrome_user_data_dir: str | None = None,
        login_wait_seconds: int = 600,
        response_timeout_seconds: int = 300,
    ) -> None:
        self.claude_url = claude_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.login_wait_seconds = login_wait_seconds
        self.response_timeout_seconds = response_timeout_seconds
        self.driver: Chrome | None = None
        self._persistent_chrome = False

    def __enter__(self) -> ClaudeRunner:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if os.environ.get("CLAUDE_PERSISTENT_CHROME", "false").lower() == "true":
            self.driver = self._connect_to_persistent_chrome()
            self._persistent_chrome = True
            if not self.headless:
                self._set_window_size()
            self.driver.get(self.claude_url)
            self.recover_chrome_error_page(context="initial_claude_load")
            self.wait_for_login()
            return

        self._persistent_chrome = False
        if self.chrome_user_data_dir:
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = Path(self.chrome_user_data_dir) / lock
                if lock_path.exists():
                    LOGGER.info("Removing stale Chrome lock: %s", lock_path)
                    lock_path.unlink(missing_ok=True)
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.clipboard": 1,
                "profile.content_settings.exceptions.clipboard": {
                    "https://claude.ai:443,*": {"setting": 1},
                },
            },
        )
        if self.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
        if self.headless:
            options.add_argument("--headless=new")

        self.driver = self.create_driver(options)
        if not self.headless:
            self._set_window_size()
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.get(self.claude_url)
        self.recover_chrome_error_page(context="initial_claude_load")
        self.wait_for_login()

    def _set_window_size(self) -> None:
        vnc_screen = os.getenv("VNC_SCREEN", "1280x720x24")
        w, h = vnc_screen.split("x")[:2]
        try:
            self.require_driver().set_window_size(int(w), int(h))
        except Exception:
            pass

    def _connect_to_persistent_chrome(self, port: int = 9222, retries: int = 5) -> Chrome:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                options = Options()
                options.debugger_address = f"localhost:{port}"
                options.add_experimental_option(
                    "prefs",
                    {
                        "profile.managed_default_content_settings.clipboard": 1,
                        "profile.content_settings.exceptions.clipboard": {
                            "https://claude.ai:443,*": {"setting": 1},
                        },
                    },
                )
                driver = webdriver.Chrome(options=options)
                LOGGER.info("Connected to persistent Chrome on port %s (attempt %s/%s).", port, attempt, retries)
                return driver
            except Exception as exc:
                last_exc = exc
                LOGGER.warning(
                    "Could not connect to persistent Chrome on port %s (attempt %s/%s): %s — retrying …",
                    port,
                    attempt,
                    retries,
                    exc,
                )
                time.sleep(3)
        raise RuntimeError(
            f"Failed to connect to persistent Chrome on port {port} after {retries} attempts: {last_exc}"
        )

    def create_driver(self, options: Options) -> Chrome:
        try:
            uc = self.import_undetected_chromedriver()
            uc_options = uc.ChromeOptions()
            for argument in options.arguments:
                if argument.startswith("--user-data-dir="):
                    continue
                uc_options.add_argument(argument)
            for experimental_option, value in options.experimental_options.items():
                uc_options.add_experimental_option(experimental_option, value)
            kwargs: dict[str, Any] = {}
            if self.chrome_user_data_dir:
                kwargs["user_data_dir"] = self.chrome_user_data_dir
            chrome_major = detect_chrome_major_version()
            if chrome_major:
                LOGGER.info("Using undetected-chromedriver for local Chrome major version %s", chrome_major)
                kwargs["version_main"] = chrome_major
            return uc.Chrome(options=uc_options, **kwargs)
        except (ImportError, ModuleNotFoundError) as error:
            LOGGER.warning(
                "undetected-chromedriver is unavailable (%s). Falling back to Selenium Chrome.",
                error,
            )
            return webdriver.Chrome(options=options)
        except SessionNotCreatedException as error:
            LOGGER.warning(
                "undetected-chromedriver could not create a session (%s). Falling back to Selenium Chrome.",
                first_line(str(error)),
            )
            return webdriver.Chrome(options=options)

    def import_undetected_chromedriver(self):
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

            LOGGER.info("Loaded undetected-chromedriver with setuptools distutils compatibility shim.")
            return uc

    def close(self) -> None:
        if self.driver:
            if self._persistent_chrome:
                LOGGER.debug("Persistent Chrome mode: releasing CDP reference, Chrome stays running.")
            else:
                self.driver.quit()
            self.driver = None

    # ── Main extraction flow ───────────────────────────────────────────────────

    def run_prompt(self, prompt_text: str) -> ClaudeCapture:
        driver = self.require_driver()
        self.create_fresh_chat()
        self._raise_if_cloudflare(context="run_prompt")
        self.dismiss_blocking_dialogs()
        input_element = self.wait_for_input()
        self.type_prompt(input_element, prompt_text)
        initial_response_count = len(self.response_elements())
        self.click_send(input_element)
        self.wait_for_response_completion(initial_response_count)
        response, markdown, markdown_capture_method = self.capture_latest_response()
        if not response or response.strip() == prompt_text.strip():
            time.sleep(2)
            response, markdown, markdown_capture_method = self.capture_latest_response()
        if not response or len(response.strip()) < 20:
            raise RuntimeError("Captured Claude response was empty or too short")
        if markdown:
            LOGGER.info(
                "Markdown copied from Claude response. length=%s method=%s",
                len(markdown.strip()),
                markdown_capture_method,
            )
        else:
            LOGGER.warning(
                "Markdown copy did not produce a valid result; markdown field will be empty. response_length=%s method=%s",
                len(response.strip()),
                markdown_capture_method,
            )
        raw_html, raw_html_capture_method = self.capture_latest_response_html()
        LOGGER.info("Raw HTML extracted from Claude response. length=%s method=%s", len(raw_html or ""), raw_html_capture_method)
        llm_model = self.capture_latest_response_model_slug() or "claude"
        LOGGER.info("Detected Claude response model. llm_model=%s", llm_model)
        sources = self.capture_latest_sources()
        source_capture_method = "inline_response_links" if sources else "none"
        if not sources:
            LOGGER.info("No inline sources captured; trying markdown reference fallback.")
            sources = extract_sources_from_markdown(markdown or response)
            source_capture_method = "markdown_references" if sources else "none"
        LOGGER.info("Captured %s source(s) using %s", len(sources), source_capture_method)
        return ClaudeCapture(
            response=response.strip(),
            markdown=markdown.strip() if markdown else "",
            capture_method="visible_text_fallback" if not markdown else "copy_button_markdown",
            markdown_capture_method=markdown_capture_method,
            raw_html=raw_html,
            raw_html_capture_method=raw_html_capture_method,
            llm_model=llm_model,
            url=driver.current_url,
            sources=sources,
            source_capture_method=source_capture_method,
            products=[],
            product_capture_method="deferred",
            entities=[],
            entity_capture_method="deferred",
        )

    # ── Login / startup wait ───────────────────────────────────────────────────

    def wait_for_login(self) -> None:
        deadline = time.time() + self.login_wait_seconds
        cf_first_seen: float | None = None
        cf_last_logged: float = 0.0
        CF_LOG_INTERVAL = 30
        cf_was_seen = False

        while time.time() < deadline:
            self.recover_chrome_error_page(context="wait_for_login")
            if self.find_first(CHAT_INPUT_SELECTORS):
                return

            cf = self.cloudflare_challenge_state()
            if cf.get("is_challenge"):
                now = time.time()
                if cf_first_seen is None:
                    cf_first_seen = now
                    cf_was_seen = True
                    LOGGER.warning(
                        "Cloudflare challenge detected on claude.ai — VNC in to solve. "
                        "title=%r url=%s signals=%s",
                        cf.get("title", ""),
                        cf.get("url", ""),
                        cf.get("signals", []),
                    )
                    cf_last_logged = now
                    notify_cloudflare_challenge(
                        signals=cf.get("signals", []),
                        title=cf.get("title", ""),
                        url=cf.get("url", ""),
                        context="wait_for_login",
                    )
                elif now - cf_last_logged >= CF_LOG_INTERVAL:
                    elapsed = int(now - cf_first_seen)
                    remaining = int(deadline - now)
                    LOGGER.warning(
                        "Cloudflare challenge still active on claude.ai — waiting. elapsed=%ss remaining=%ss",
                        elapsed,
                        remaining,
                    )
                    cf_last_logged = now
            else:
                if cf_first_seen is not None:
                    elapsed = int(time.time() - cf_first_seen)
                    LOGGER.info("Cloudflare challenge cleared after %ss — resuming.", elapsed)
                    notify_cloudflare_cleared(elapsed_seconds=elapsed, context="wait_for_login")
                cf_first_seen = None

            time.sleep(1)

        if cf_was_seen:
            raise TimeoutError(
                "Timed out waiting for claude.ai prompt input — Cloudflare challenge was blocking. "
                "VNC in and solve the challenge, then re-queue the run."
            )
        raise TimeoutError(
            "Timed out waiting for claude.ai prompt input. "
            "Ensure CLAUDE_CHROME_USER_DATA_DIR points to a logged-in Chrome profile."
        )

    # ── Navigation ─────────────────────────────────────────────────────────────

    def create_fresh_chat(self) -> None:
        self.require_driver().get(NEW_CHAT_URL)
        self.recover_chrome_error_page(context="create_fresh_chat")
        time.sleep(random.uniform(2.0, 4.0))

    # ── Input interaction ──────────────────────────────────────────────────────

    def wait_for_input(self) -> WebElement:
        deadline = time.time() + 30
        while time.time() < deadline:
            self.dismiss_blocking_dialogs()
            element = self.wait_for_clickable(By.CSS_SELECTOR, CHAT_INPUT_SELECTOR, timeout=2)
            if element and element.is_displayed() and element.is_enabled():
                return element
            # Also try each selector individually in case the composite misses
            element = self.find_first(CHAT_INPUT_SELECTORS)
            if element and element.is_displayed() and element.is_enabled():
                return element
            time.sleep(0.5)
        raise TimeoutException("Could not find Claude.ai input field within 30 seconds")

    def type_prompt(self, input_element: WebElement, prompt_text: str) -> None:
        self.focus_input(input_element)

        # Clear any existing content
        select_modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        input_element.send_keys(select_modifier, "a")
        input_element.send_keys(Keys.BACKSPACE)
        time.sleep(0.1)

        # Type the first word character-by-character to trigger ProseMirror's
        # input detection, then insert the remainder via execCommand.
        words = prompt_text.split(" ", 1)
        first_word = words[0]
        remainder = (" " + words[1]) if len(words) > 1 else ""

        for char in first_word:
            if char == "\n":
                input_element.send_keys(Keys.SHIFT, Keys.ENTER)
            else:
                input_element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.12))

        if remainder:
            if not self._js_insert_at_cursor(input_element, remainder):
                LOGGER.warning("Fast JS insert failed; falling back to character-by-character.")
                for char in remainder:
                    if char == "\n":
                        input_element.send_keys(Keys.SHIFT, Keys.ENTER)
                    else:
                        input_element.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))

        time.sleep(0.15)

    def _js_insert_at_cursor(self, input_element: WebElement, text: str) -> bool:
        try:
            result = self.require_driver().execute_script(
                """
                const el = arguments[0];
                const text = arguments[1];
                el.focus();
                return document.execCommand('insertText', false, text);
                """,
                input_element,
                text,
            )
            return bool(result)
        except (WebDriverException, JavascriptException):
            return False

    def click_send(self, input_element: WebElement) -> None:
        self.dismiss_blocking_dialogs()
        for selector in SEND_BUTTON_SELECTORS:
            button = self.wait_for_clickable(By.CSS_SELECTOR, selector, timeout=3)
            if button and button.is_enabled():
                self.click_element(button)
                return
        # Fallback: Enter key (Shift+Enter is newline in Claude)
        input_element.send_keys(Keys.ENTER)

    def focus_input(self, input_element: WebElement) -> None:
        driver = self.require_driver()
        self.dismiss_blocking_dialogs()
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", input_element)
        time.sleep(0.2)
        try:
            input_element.click()
        except ElementClickInterceptedException:
            self.dismiss_blocking_dialogs()
            driver.execute_script("arguments[0].focus();", input_element)
            time.sleep(0.2)

    def dismiss_blocking_dialogs(self) -> None:
        driver = self.require_driver()
        try:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except WebDriverException:
            pass

        selectors = [
            "button[aria-label='Close']",
            "button[aria-label='Dismiss']",
            "button[data-testid*='close']",
            "button[data-testid*='dismiss']",
        ]
        for selector in selectors:
            for button in driver.find_elements(By.CSS_SELECTOR, selector):
                if self.click_if_visible(button):
                    time.sleep(0.5)
                    return

        for button in driver.find_elements(By.CSS_SELECTOR, "button"):
            label = (
                " ".join(
                    value.strip()
                    for value in [
                        button.text or "",
                        button.get_attribute("aria-label") or "",
                        button.get_attribute("title") or "",
                    ]
                    if value and value.strip()
                )
                .strip()
                .lower()
            )
            if label in DISMISS_BUTTON_TEXT and self.click_if_visible(button):
                time.sleep(0.5)
                return

    # ── Response wait ──────────────────────────────────────────────────────────

    def wait_for_response_completion(self, initial_response_count: int) -> None:
        stop_button_seen = self.wait_for_stop_button(timeout=30)
        if stop_button_seen:
            if self.wait_for_stop_button_to_disappear(timeout=self.response_timeout_seconds):
                time.sleep(2)
                return
            raise TimeoutError(f"Claude stop button did not disappear. {self.collect_page_signals()}")

        LOGGER.warning(
            "Claude stop button did not appear after submit. Falling back to response stability wait. %s",
            self.collect_page_signals(),
        )

        deadline = time.time() + self.response_timeout_seconds
        last_text = ""
        stable_checks = 0

        while time.time() < deadline:
            stop_button = self.find_first(STOP_BUTTON_SELECTORS)
            latest_text = self.latest_response_text()
            if not stop_button and latest_text:
                if latest_text == last_text:
                    stable_checks += 1
                else:
                    stable_checks = 0
                    last_text = latest_text
                if stable_checks >= 3:
                    return
            time.sleep(2)

        raise TimeoutError("Timed out waiting for Claude response to complete")

    def wait_for_stop_button(self, timeout: int) -> bool:
        for selector in STOP_BUTTON_SELECTORS:
            if self.wait_for_clickable(By.CSS_SELECTOR, selector, timeout=timeout):
                return True
        return False

    def wait_for_stop_button_to_disappear(self, timeout: int) -> bool:
        def all_stop_buttons_gone(driver) -> bool:
            for selector in STOP_BUTTON_SELECTORS:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if any(el.is_displayed() for el in elements):
                        return False
                except WebDriverException:
                    pass
            return True

        try:
            WebDriverWait(self.require_driver(), timeout).until(all_stop_buttons_gone)
            return True
        except TimeoutException:
            return False

    def collect_page_signals(self) -> str:
        try:
            body_text = self.require_driver().find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            return "No page text available."
        keywords = ["something went wrong", "unusual activity", "verify", "captcha", "network error", "try again", "rate limit", "limit reached"]
        lowered = body_text.lower()
        matches = [kw for kw in keywords if kw in lowered]
        if matches:
            return f"Visible page signals: {', '.join(matches)}"
        return "No obvious blocking page text detected."

    # ── Response capture ───────────────────────────────────────────────────────

    def capture_latest_response(self) -> tuple[str, str, str]:
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "", "no_response_element"

        visible_text = latest_response.text or ""

        # Find the copy button relative to the response element
        copy_button = self._find_copy_button_for_response(latest_response)
        if not copy_button:
            return visible_text, "", "copy_button_not_found"

        for attempt in range(1, 4):
            try:
                marker = f"__brandsight_capture_marker_{time.time_ns()}__"
                pyperclip.copy(marker)
                self.click_element(copy_button)
                deadline = time.time() + 5
                while time.time() < deadline:
                    copied = pyperclip.paste()
                    if copied and copied != marker:
                        self._kill_xclip_orphans()
                        return visible_text, copied, f"copy_button_markdown_attempt_{attempt}"
                    time.sleep(0.2)
                LOGGER.info("Markdown copy attempt did not produce clipboard payload. attempt=%s/3", attempt)
                time.sleep(1)
            except WebDriverException as exc:
                LOGGER.warning("Markdown copy attempt failed. attempt=%s/3 error=%s", attempt, first_line(str(exc)))
                time.sleep(1)

        self._kill_xclip_orphans()
        return visible_text, "", "copy_button_markdown_failed_after_retries"

    def _find_copy_button_for_response(self, response: WebElement) -> WebElement | None:
        driver = self.require_driver()
        # Hover over response to reveal action buttons
        try:
            ActionChains(driver).move_to_element(response).perform()
            time.sleep(0.4)
        except WebDriverException:
            pass

        # Fast path: Claude.ai uses data-testid="action-bar-copy" on the copy button.
        # Pick the last visible one (latest AI response).
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, "[data-testid='action-bar-copy']")
            for btn in reversed(btns):
                if btn.is_displayed() and btn.is_enabled():
                    return btn
        except WebDriverException:
            pass

        # Search up the DOM a few levels for a container with action buttons
        for xpath in [".", "..", "../..", "../../..", "../../../.."]:
            try:
                container = response.find_element(By.XPATH, xpath)
                button = self._find_copy_button_in_container(container)
                if button:
                    return button
            except (NoSuchElementException, WebDriverException):
                continue
        return None

    def _find_copy_button_in_container(self, container: WebElement) -> WebElement | None:
        try:
            buttons = container.find_elements(By.CSS_SELECTOR, "button")
        except WebDriverException:
            return None
        for button in reversed(buttons):
            try:
                label = " ".join(
                    filter(
                        None,
                        [
                            button.get_attribute("aria-label") or "",
                            button.get_attribute("data-testid") or "",
                            button.get_attribute("title") or "",
                            button.text or "",
                        ],
                    )
                ).lower()
                html = button.get_attribute("outerHTML") or ""
                testid = button.get_attribute("data-testid") or ""
                if "copy" in label or "copy" in testid or ("clip-rule" in html and "M7 5a3 3" in html):
                    if button.is_displayed() and button.is_enabled():
                        return button
            except WebDriverException:
                continue
        return None

    @staticmethod
    def _kill_xclip_orphans() -> None:
        import platform as _platform
        import subprocess as _sub

        if _platform.system() != "Linux":
            return
        try:
            result = _sub.run(["pkill", "-x", "xclip"], capture_output=True, timeout=5)
            if result.returncode == 0:
                LOGGER.debug("Killed lingering xclip process(es).")
        except Exception as exc:
            LOGGER.debug("Could not kill xclip orphans: %s", exc)

    def capture_latest_response_html(self) -> tuple[str, str]:
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "none"
        html = self.element_outer_html(latest_response)
        if html.strip():
            return html, "assistant_message_outer_html"
        return "", "none"

    def capture_latest_response_model_slug(self) -> str:
        """
        Claude.ai doesn't embed the model in a data attribute like ChatGPT does.
        We read it from the model selector button text or the page title.
        """
        driver = self.require_driver()
        try:
            # Claude often shows the model name in a button or selector in the header
            model_text = driver.execute_script(
                """
                const selectors = [
                    'button[data-testid="model-selector-dropdown"]',
                    'button[data-testid="model-selector-trigger"]',
                    'button[data-testid="model-selector"]',
                    '[aria-label*="Claude"] button',
                    '.model-selector button',
                    'button[class*="model"]',
                    '[data-testid*="model"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    const text = (el?.innerText || el?.textContent || '').trim();
                    if (text) return text;
                }
                // Fallback: look for Claude model name in any visible button text
                const buttons = document.querySelectorAll('button, [role="button"]');
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim();
                    if (/claude[\\s-]*(sonnet|opus|haiku|instant)/i.test(text)) return text;
                }
                return '';
                """
            )
            if model_text:
                return clean_text(str(model_text))
        except WebDriverException:
            pass

        # Final fallback: parse document title
        try:
            title = driver.title or ""
            match = re.search(r"Claude\s+(Sonnet|Opus|Haiku|Instant)[^\s]*", title, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        except WebDriverException:
            pass

        return "claude"

    # ── Source capture ─────────────────────────────────────────────────────────

    def capture_latest_sources(self) -> list[dict[str, Any]]:
        """
        Extract inline citation links from the latest Claude response.
        Claude embeds sources as <a href> links within the response body.
        """
        latest_response = self.latest_response_element()
        if not latest_response:
            LOGGER.info("Skipping source capture: no latest response element found.")
            return []

        try:
            raw_sources = self.require_driver().execute_script(
                """
                const response = arguments[0];
                const links = [...response.querySelectorAll('a[href]')].filter(
                    link => /^https?:\\/\\//.test(link.href)
                );
                return links.map((link, index) => ({
                    index: index + 1,
                    url: link.href,
                    title: (link.innerText || link.textContent || '').trim(),
                    source: (new URL(link.href)).hostname.replace('www.', ''),
                    description: link.getAttribute('title') || '',
                    favicon_url: ''
                }));
                """,
                latest_response,
            )
        except (WebDriverException, JavascriptException):
            return []

        if not isinstance(raw_sources, list):
            return []

        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url or raw_url in seen_urls or not raw_url.startswith("http"):
                continue
            seen_urls.add(raw_url)
            sources.append(
                {
                    "index": len(sources) + 1,
                    "url": raw_url,
                    "clean_url": raw_url,
                    "source": clean_text(item.get("source")),
                    "title": clean_text(item.get("title")),
                    "description": clean_text(item.get("description")),
                    "favicon_url": None,
                    "extraction_source": "inline_response_links",
                    "source_group": "primary",
                    "is_more_source": False,
                }
            )
        return sources

    # ── Response element helpers ───────────────────────────────────────────────

    def latest_response_text(self) -> str:
        element = self.latest_response_element()
        return element.text.strip() if element else ""

    def latest_response_element(self) -> WebElement | None:
        for attempt in range(3):
            try:
                responses = self.response_elements()
                return responses[-1] if responses else None
            except StaleElementReferenceException:
                if attempt < 2:
                    time.sleep(0.5)
        return None

    def response_elements(self) -> list[WebElement]:
        driver = self.require_driver()
        for selector in ASSISTANT_RESPONSE_SELECTORS:
            try:
                elements = [
                    el for el in driver.find_elements(By.CSS_SELECTOR, selector)
                    if el.is_displayed()
                ]
                if elements:
                    return elements
            except WebDriverException:
                continue
        return []

    def element_outer_html(self, element: WebElement) -> str:
        try:
            html = self.require_driver().execute_script("return arguments[0].outerHTML || '';", element)
            return str(html or "")
        except WebDriverException:
            return ""

    # ── Cloudflare detection ───────────────────────────────────────────────────

    def cloudflare_challenge_state(self) -> dict[str, Any]:
        try:
            result = self.require_driver().execute_script(
                """
                const title = (document.title || '').toLowerCase();
                const signals = [];

                if (/just a moment|are you human|verify.*human|human.*verify/i.test(document.title))
                    signals.push('title_challenge');

                if (document.querySelector('#challenge-running, #challenge-form, .cf-browser-verification, #cf-challenge-running'))
                    signals.push('cf_element');

                if (document.querySelector('iframe[src*="challenges.cloudflare.com"]'))
                    signals.push('cf_turnstile_iframe');

                const hasCFScript = Array.from(document.scripts).some(
                    s => s.src && s.src.includes('cloudflare.com')
                );
                if (hasCFScript) signals.push('cf_script');

                if (/^just a moment/i.test(document.title) && !document.querySelector('[data-testid]'))
                    signals.push('title_just_a_moment_no_testid');

                return {
                    is_challenge: signals.length > 0,
                    title: document.title,
                    url: location.href,
                    signals: signals,
                };
                """
            )
            return result if isinstance(result, dict) else {"is_challenge": False}
        except WebDriverException:
            return {"is_challenge": False}

    def _raise_if_cloudflare(self, *, context: str) -> None:
        cf = self.cloudflare_challenge_state()
        if cf.get("is_challenge"):
            LOGGER.warning(
                "Cloudflare challenge detected during %s — VNC in to resolve. "
                "title=%r url=%s signals=%s",
                context,
                cf.get("title", ""),
                cf.get("url", ""),
                cf.get("signals", []),
            )
            notify_cloudflare_challenge(
                signals=cf.get("signals", []),
                title=cf.get("title", ""),
                url=cf.get("url", ""),
                context=context,
            )

    # ── Chrome error page recovery ─────────────────────────────────────────────

    def recover_chrome_error_page(self, *, context: str, max_attempts: int = 2) -> bool:
        driver = self.require_driver()
        recovered = False
        for attempt in range(1, max_attempts + 1):
            state = self.chrome_error_page_state()
            if not state.get("is_error"):
                return recovered
            LOGGER.warning(
                "Detected Chrome error page during %s. attempt=%s/%s error_code=%s",
                context,
                attempt,
                max_attempts,
                state.get("error_code") or "<unknown>",
            )
            reload_button = self.find_first(["#reload-button", "button[data-url]", "button"])
            if reload_button and self.click_if_visible(reload_button):
                LOGGER.info("Clicked Chrome error page reload button during %s.", context)
            else:
                reload_url = str(state.get("reload_url") or "").strip()
                if reload_url:
                    driver.get(reload_url)
                else:
                    driver.refresh()
            recovered = True
            time.sleep(3)
        return recovered

    def chrome_error_page_state(self) -> dict[str, Any]:
        try:
            result = self.require_driver().execute_script(
                """
                const body = document.body;
                const mainFrameError = document.querySelector('#main-frame-error');
                const errorCode = (document.querySelector('.error-code')?.innerText || '').trim();
                const heading = (document.querySelector('#main-message h1, h1')?.innerText || '').trim();
                const reloadButton = document.querySelector('#reload-button, button[data-url]');
                const bodyClass = body?.className || '';
                const isChromeNetError = Boolean(
                  mainFrameError ||
                  bodyClass.includes('neterror') ||
                  window.loadTimeDataRaw?.errorCode ||
                  /^HTTP ERROR \\d+/i.test(errorCode)
                );
                const isClaudeError = /claude\\.ai/i.test(document.title || location.href || '');
                return {
                  is_error: Boolean(isChromeNetError && isClaudeError),
                  error_code: errorCode || window.loadTimeDataRaw?.errorCode || '',
                  heading,
                  reload_url: reloadButton?.dataset?.url || window.loadTimeDataRaw?.reloadButton?.reloadUrl || '',
                  has_reload_button: Boolean(reloadButton),
                };
                """
            )
            return result if isinstance(result, dict) else {"is_error": False}
        except WebDriverException:
            return {"is_error": False}

    # ── Click / wait helpers ───────────────────────────────────────────────────

    def click_element(self, element: WebElement) -> None:
        driver = self.require_driver()
        time.sleep(1)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        time.sleep(0.2)
        try:
            ActionChains(driver).move_to_element(element).click().perform()
        except (ElementClickInterceptedException, JavascriptException, WebDriverException):
            driver.execute_script(
                """
                const element = arguments[0];
                element.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'mouse'}));
                element.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                element.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'mouse'}));
                element.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                element.click();
                """,
                element,
            )

    def click_if_visible(self, element: WebElement) -> bool:
        try:
            if not element.is_displayed() or not element.is_enabled():
                return False
            try:
                element.click()
            except ElementClickInterceptedException:
                self.require_driver().execute_script("arguments[0].click();", element)
            return True
        except WebDriverException:
            return False

    def wait_for_clickable(self, by: str, selector: str, timeout: int = 10) -> WebElement | None:
        try:
            return WebDriverWait(self.require_driver(), timeout).until(EC.element_to_be_clickable((by, selector)))
        except TimeoutException:
            return None

    def find_first(self, selectors: list[str]) -> WebElement | None:
        driver = self.require_driver()
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except WebDriverException:
                continue
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except WebDriverException:
                    continue
        return None

    def require_driver(self) -> Chrome:
        if not self.driver:
            raise RuntimeError("ClaudeRunner: browser has not been started")
        return self.driver


# ── Module-level helpers (shared with chatgpt_runner pattern) ──────────────────

def detect_chrome_major_version() -> int | None:
    if platform.system() == "Darwin":
        commands = [
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
            ["/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary", "--version"],
        ]
    elif platform.system() == "Windows":
        commands = [["chrome", "--version"]]
    else:
        commands = [["google-chrome", "--version"], ["google-chrome-stable", "--version"], ["chromium", "--version"]]

    for command in commands:
        try:
            output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    return None


def first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_sources_from_markdown(markdown: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    reference_pattern = re.compile(
        r"""^\s*\[(?P<ref>[^\]]+)\]:\s+(?P<url>\S+)(?:\s+(?P<title>"[^"]*"|'[^']*'|\([^)]+\)))?\s*$""",
        re.MULTILINE,
    )
    for match in reference_pattern.finditer(markdown or ""):
        raw_url = match.group("url").strip()
        if not _is_external_url(raw_url) or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        title_raw = match.group("title") or ""
        title = re.sub(r'^["\'\(]|["\'\)]$', "", title_raw).strip()
        sources.append(
            {
                "index": len(sources) + 1,
                "url": raw_url,
                "clean_url": raw_url,
                "source": urlsplit(raw_url).netloc.lstrip("www.") if raw_url.startswith("http") else "",
                "title": title,
                "description": "",
                "favicon_url": None,
                "extraction_source": "markdown_references",
                "source_group": "primary",
                "is_more_source": False,
            }
        )

    inline_pattern = re.compile(
        r"""(?<!!)\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)(?:\s+(?P<title>"[^"]*"|'[^']*'))?\)"""
    )
    for match in inline_pattern.finditer(markdown or ""):
        raw_url = match.group("url").strip().rstrip(")")
        if not _is_external_url(raw_url) or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        title_raw = match.group("title") or match.group("label") or ""
        title = re.sub(r'^["\']|["\']$', "", title_raw).strip()
        sources.append(
            {
                "index": len(sources) + 1,
                "url": raw_url,
                "clean_url": raw_url,
                "source": urlsplit(raw_url).netloc.lstrip("www.") if raw_url.startswith("http") else "",
                "title": title,
                "description": "",
                "favicon_url": None,
                "extraction_source": "markdown_inline_links",
                "source_group": "primary",
                "is_more_source": False,
            }
        )

    return sources


def _is_external_url(url: str) -> bool:
    return bool(url and url.startswith(("http://", "https://")))
