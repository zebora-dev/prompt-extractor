from __future__ import annotations

import logging
import os
import platform
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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

# Perplexity uses a plain <textarea> for input (with Lexical contenteditable as fallback).
CHAT_INPUT_SELECTORS = [
    "textarea[placeholder*='Ask']",
    "textarea[placeholder*='Search']",
    "textarea[placeholder*='ask']",
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][aria-label*='Ask']",
    "textarea:not([disabled]):not([readonly])",
]

SEND_BUTTON_SELECTORS = [
    "button[aria-label='Submit']",
    "button[aria-label='Send']",
    "button[aria-label*='Submit']",
    "button[aria-label*='submit']",
    "button[type='submit']",
]

# Perplexity re-enables the submit button when response is complete.
# We poll for it NOT being disabled as the primary completion signal.
SUBMIT_BUTTON_SELECTORS = SEND_BUTTON_SELECTORS

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

NEW_CHAT_URL = "https://www.perplexity.ai/"

# Perplexity renders answers in Tailwind .prose containers.
ASSISTANT_RESPONSE_SELECTORS = [
    ".prose",
    "[class*='prose']",
    "div[class*='answer']",
    "[data-testid='answer']",
    "div[class*='AnswerBody']",
]

CHAT_INPUT_SELECTOR = ", ".join(CHAT_INPUT_SELECTORS[:4])


@dataclass
class PerplexityCapture:
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


class PerplexityRunner:
    def __init__(
        self,
        perplexity_url: str,
        *,
        headless: bool = False,
        chrome_user_data_dir: str | None = None,
        login_wait_seconds: int = 600,
        response_timeout_seconds: int = 300,
    ) -> None:
        self.perplexity_url = perplexity_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.login_wait_seconds = login_wait_seconds
        self.response_timeout_seconds = response_timeout_seconds
        self.driver: Chrome | None = None

    def __enter__(self) -> PerplexityRunner:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        if self.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
        if self.headless:
            options.add_argument("--headless=new")

        self.driver = self.create_driver(options)
        if not self.headless:
            self._set_window_size()
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.get(self.perplexity_url)
        self.recover_chrome_error_page(context="initial_perplexity_load")
        self.wait_for_login()

    def _set_window_size(self) -> None:
        vnc_screen = os.getenv("VNC_SCREEN", "1280x720x24")
        w, h = vnc_screen.split("x")[:2]
        try:
            self.require_driver().set_window_size(int(w), int(h))
        except Exception:
            pass

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
                LOGGER.info("Using undetected-chromedriver for Chrome major version %s", chrome_major)
                kwargs["version_main"] = chrome_major
            return uc.Chrome(options=uc_options, **kwargs)
        except (ImportError, ModuleNotFoundError) as error:
            LOGGER.warning("undetected-chromedriver unavailable (%s). Falling back to Selenium Chrome.", error)
            return webdriver.Chrome(options=options)
        except SessionNotCreatedException as error:
            LOGGER.warning(
                "undetected-chromedriver session failed (%s). Falling back to Selenium Chrome.",
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
            self.driver.quit()
            self.driver = None

    # ── Main extraction flow ───────────────────────────────────────────────────

    def run_prompt(self, prompt_text: str) -> PerplexityCapture:
        driver = self.require_driver()
        self.create_fresh_chat()
        self._raise_if_cloudflare(context="run_prompt")
        self.dismiss_blocking_dialogs()
        input_element = self.wait_for_input()
        self.type_prompt(input_element, prompt_text)
        self.click_send(input_element)
        self.wait_for_response_completion()
        response, raw_html, raw_html_capture_method = self.capture_latest_response()
        if not response or response.strip() == prompt_text.strip():
            time.sleep(2)
            response, raw_html, raw_html_capture_method = self.capture_latest_response()
        if not response or len(response.strip()) < 20:
            raise RuntimeError("Captured Perplexity response was empty or too short")
        LOGGER.info("Perplexity response captured. length=%s method=%s", len(response.strip()), raw_html_capture_method)
        llm_model = self.capture_latest_response_model_slug() or "perplexity"
        LOGGER.info("Detected Perplexity model. llm_model=%s", llm_model)
        sources = self.capture_latest_sources()
        source_capture_method = "inline_response_links" if sources else "none"
        if not sources:
            sources = extract_sources_from_markdown(response)
            source_capture_method = "markdown_references" if sources else "none"
        LOGGER.info("Captured %s source(s) using %s", len(sources), source_capture_method)
        return PerplexityCapture(
            response=response.strip(),
            markdown=response.strip(),  # plain text = markdown for Perplexity (no copy button needed)
            capture_method="visible_text",
            markdown_capture_method="visible_text",
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
                        "Cloudflare challenge detected on perplexity.ai — VNC in to solve. "
                        "title=%r url=%s signals=%s",
                        cf.get("title", ""), cf.get("url", ""), cf.get("signals", []),
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
                    LOGGER.warning(
                        "Cloudflare challenge still active — elapsed=%ss remaining=%ss",
                        elapsed, int(deadline - now),
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
                "Timed out waiting for perplexity.ai prompt input — Cloudflare challenge was blocking."
            )
        raise TimeoutError(
            "Timed out waiting for perplexity.ai prompt input. "
            "Ensure PERPLEXITY_CHROME_USER_DATA_DIR points to a logged-in Chrome profile."
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
            element = self.find_first(CHAT_INPUT_SELECTORS)
            if element and element.is_displayed() and element.is_enabled():
                return element
            time.sleep(0.5)
        raise TimeoutException("Could not find Perplexity input field within 30 seconds")

    def type_prompt(self, input_element: WebElement, prompt_text: str) -> None:
        self.focus_input(input_element)
        tag = (input_element.tag_name or "").lower()

        if tag == "textarea":
            # Plain textarea — clear and send_keys works directly.
            input_element.clear()
            time.sleep(0.1)
            # Type first few chars to trigger any React input listeners, then send rest
            first_chars = prompt_text[:5]
            remainder = prompt_text[5:]
            for char in first_chars:
                input_element.send_keys(char)
                time.sleep(random.uniform(0.05, 0.12))
            if remainder:
                input_element.send_keys(remainder)
        else:
            # Lexical contenteditable — use ActionChains
            select_modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
            input_element.send_keys(select_modifier, "a")
            input_element.send_keys(Keys.BACKSPACE)
            time.sleep(0.1)
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
        # Fallback: Enter key
        input_element.send_keys(Keys.RETURN)

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
        # Use JS to find and click dismiss buttons in one shot to avoid stale elements
        try:
            dismiss_labels = list(DISMISS_BUTTON_TEXT)
            clicked = driver.execute_script(
                """
                const labels = arguments[0];
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const label = [
                        btn.getAttribute('aria-label') || '',
                        btn.getAttribute('title') || '',
                        btn.innerText || '',
                    ].join(' ').trim().toLowerCase();
                    if (labels.some(l => label.includes(l))) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
                """,
                dismiss_labels,
            )
            if clicked:
                time.sleep(0.5)
        except (WebDriverException, JavascriptException):
            pass

    # ── Response wait ──────────────────────────────────────────────────────────

    def wait_for_response_completion(self) -> None:
        """
        Wait for Perplexity's response to finish.

        Perplexity's submit button stays disabled even after completion (it only
        enables when text is typed in the box), so we cannot use its re-enable
        state as a signal. Instead we watch for the post-answer action buttons
        ("Helpful", "Not helpful", "Copy", "Rewrite Session") which only appear
        once a response is fully rendered.

        Fallback: .prose text stabilises over 3 consecutive 2s polls.
        """
        # Brief initial wait to let the searching phase begin
        time.sleep(3)

        # Confirm streaming started: submit button goes disabled (or prose appears)
        submit_disabled_deadline = time.time() + 15
        while time.time() < submit_disabled_deadline:
            if self._is_submit_disabled() or self.latest_response_text():
                LOGGER.info("Perplexity response started (submit disabled or prose visible).")
                break
            time.sleep(0.5)

        # Primary completion signal: post-answer action buttons appear
        deadline = time.time() + self.response_timeout_seconds
        while time.time() < deadline:
            if self._answer_actions_visible():
                LOGGER.info("Perplexity answer action buttons detected — response complete.")
                time.sleep(0.5)
                return
            time.sleep(1)

        # Fallback: prose text stability (3 identical reads 2s apart)
        LOGGER.warning(
            "Answer action buttons never appeared. Falling back to text stability. %s",
            self.collect_page_signals(),
        )
        deadline = time.time() + 30
        last_text = ""
        stable_checks = 0
        while time.time() < deadline:
            latest_text = self.latest_response_text()
            if latest_text:
                if latest_text == last_text:
                    stable_checks += 1
                else:
                    stable_checks = 0
                    last_text = latest_text
                if stable_checks >= 3:
                    return
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for Perplexity response. {self.collect_page_signals()}")

    def _is_submit_disabled(self) -> bool:
        """Return True if the submit button is currently disabled (response in progress)."""
        driver = self.require_driver()
        try:
            result = driver.execute_script(
                """
                const selectors = [
                    'button[aria-label="Submit"]',
                    'button[aria-label="Send"]',
                    'button[type="submit"]',
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) {
                        return btn.disabled || btn.getAttribute('aria-disabled') === 'true'
                            || btn.getAttribute('data-disabled') === 'true';
                    }
                }
                return false;
                """
            )
            return bool(result)
        except (WebDriverException, JavascriptException):
            return False

    def _answer_actions_visible(self) -> bool:
        """
        Return True when the post-answer action buttons are in the DOM.
        These buttons (Helpful, Not helpful, Copy, Rewrite Session) only appear
        once Perplexity has fully rendered the response.
        """
        driver = self.require_driver()
        try:
            return bool(driver.execute_script(
                """
                const labels = ['Helpful', 'Not helpful', 'Copy', 'Rewrite Session'];
                return labels.some(label => !!document.querySelector(
                    `button[aria-label="${label}"]`
                ));
                """
            ))
        except (WebDriverException, JavascriptException):
            return False

    def collect_page_signals(self) -> str:
        try:
            body_text = self.require_driver().find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            return "No page text available."
        keywords = ["something went wrong", "unusual activity", "verify", "captcha", "network error", "try again", "rate limit"]
        lowered = body_text.lower()
        matches = [kw for kw in keywords if kw in lowered]
        if matches:
            return f"Visible page signals: {', '.join(matches)}"
        return "No obvious blocking page text detected."

    # ── Response capture ───────────────────────────────────────────────────────

    def capture_latest_response(self) -> tuple[str, str, str]:
        """
        Returns (visible_text, raw_html, capture_method).
        Perplexity doesn't have a stable copy button — read directly from DOM.
        """
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "", "no_response_element"
        try:
            visible_text = latest_response.text or ""
            raw_html = self.element_outer_html(latest_response)
            return visible_text, raw_html, "prose_innerText"
        except StaleElementReferenceException:
            return "", "", "stale_element"

    def capture_latest_response_html(self) -> tuple[str, str]:
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "none"
        html = self.element_outer_html(latest_response)
        if html.strip():
            return html, "prose_outer_html"
        return "", "none"

    def capture_latest_response_model_slug(self) -> str:
        """
        Read the active model from the model selector dropdown or page.
        Perplexity shows the current model in a button/selector near the input.
        """
        driver = self.require_driver()
        try:
            model_text = driver.execute_script(
                """
                const selectors = [
                    '[data-testid="model-selector"]',
                    '[data-testid*="model"]',
                    'button[aria-haspopup="listbox"]',
                    'button[aria-haspopup="menu"]',
                    '[class*="ModelSelector"] button',
                    '[class*="model-selector"] button',
                    '[class*="ModelButton"]',
                    'button[class*="model"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    const text = (el?.innerText || el?.textContent || '').trim();
                    if (text && text.length < 80) return text;
                }
                // Fallback: look for known model name patterns in visible text
                const allButtons = document.querySelectorAll('button, [role="button"]');
                for (const btn of allButtons) {
                    const text = (btn.innerText || '').trim();
                    if (/sonar|perplexity|claude|gpt|gemini|llama/i.test(text) && text.length < 80) {
                        return text;
                    }
                }
                return '';
                """
            )
            if model_text:
                return clean_text(str(model_text))
        except WebDriverException:
            pass

        try:
            title = driver.title or ""
            match = re.search(r"(Sonar|Perplexity|Claude|GPT|Gemini|Llama)[^\s|–-]*", title, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        except WebDriverException:
            pass

        return "perplexity-sonar"

    # ── Source capture ─────────────────────────────────────────────────────────

    def capture_latest_sources(self) -> list[dict[str, Any]]:
        """
        Capture sources from the Perplexity Links tab.

        Perplexity groups Answer / Links / Images in tabs. Sources are in the
        Links tab. We click it, wait briefly, then scrape all external anchors
        that appeared. Inline prose citations are also captured if present.
        """
        driver = self.require_driver()

        # Click the Links tab to load source links
        try:
            clicked = driver.execute_script(
                """
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => (b.innerText || '').trim() === 'Links');
                if (btn) { btn.click(); return true; }
                return false;
                """
            )
            if clicked:
                time.sleep(1.5)
        except (WebDriverException, JavascriptException):
            pass

        try:
            raw_sources = driver.execute_script(
                """
                const results = [];
                const seen = new Set();
                const excluded = /perplexity\\.ai/;

                // All external links visible after clicking Links tab
                document.querySelectorAll('a[href]').forEach(anchor => {
                    const url = anchor.href;
                    if (!url || !url.startsWith('http') || excluded.test(url) || seen.has(url)) return;
                    seen.add(url);
                    let hostname = '';
                    try { hostname = new URL(url).hostname.replace('www.', ''); } catch(e) {}
                    results.push({
                        url,
                        title: (anchor.innerText || anchor.textContent || '').trim().slice(0, 200),
                        source: hostname,
                        description: '',
                        extraction_source: 'links_tab',
                    });
                });

                // Also inline prose citations (sup > a)
                const proseEls = document.querySelectorAll('.prose, [class*="prose"]');
                const latestProse = proseEls[proseEls.length - 1];
                if (latestProse) {
                    latestProse.querySelectorAll('sup a[href], a[href]').forEach(a => {
                        const url = a.href;
                        if (!url || !url.startsWith('http') || excluded.test(url) || seen.has(url)) return;
                        seen.add(url);
                        let hostname = '';
                        try { hostname = new URL(url).hostname.replace('www.', ''); } catch(e) {}
                        results.push({
                            url,
                            title: (a.innerText || '').trim(),
                            source: hostname,
                            description: '',
                            extraction_source: 'prose_inline',
                        });
                    });
                }
                return results;
                """
            )
        except (WebDriverException, JavascriptException):
            return []

        if not isinstance(raw_sources, list):
            return []

        sources: list[dict[str, Any]] = []
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url or not raw_url.startswith("http"):
                continue
            sources.append(
                {
                    "index": len(sources) + 1,
                    "url": raw_url,
                    "clean_url": raw_url,
                    "source": clean_text(item.get("source")),
                    "title": clean_text(item.get("title")),
                    "description": clean_text(item.get("description")),
                    "favicon_url": None,
                    "extraction_source": item.get("extraction_source", "perplexity"),
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
                context, cf.get("title", ""), cf.get("url", ""), cf.get("signals", []),
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
                context, attempt, max_attempts, state.get("error_code") or "<unknown>",
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
                const mainFrameError = document.querySelector('#main-frame-error');
                const errorCode = (document.querySelector('.error-code')?.innerText || '').trim();
                const reloadButton = document.querySelector('#reload-button, button[data-url]');
                const bodyClass = document.body?.className || '';
                const isChromeNetError = Boolean(
                    mainFrameError || bodyClass.includes('neterror')
                    || window.loadTimeDataRaw?.errorCode
                );
                return {
                    is_error: Boolean(isChromeNetError),
                    error_code: errorCode || window.loadTimeDataRaw?.errorCode || '',
                    reload_url: reloadButton?.dataset?.url || '',
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
                const el = arguments[0];
                el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'mouse'}));
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'mouse'}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                el.click();
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
            raise RuntimeError("PerplexityRunner: browser has not been started")
        return self.driver


# ── Module-level helpers ───────────────────────────────────────────────────────

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


def extract_sources_from_markdown(text: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    inline_pattern = re.compile(
        r"""(?<!!)\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)(?:\s+(?P<title>"[^"]*"|'[^']*'))?\)"""
    )
    for match in inline_pattern.finditer(text or ""):
        raw_url = match.group("url").strip().rstrip(")")
        if not raw_url.startswith("http") or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        title_raw = match.group("title") or match.group("label") or ""
        title = re.sub(r'^["\']|["\']$', "", title_raw).strip()
        sources.append(
            {
                "index": len(sources) + 1,
                "url": raw_url,
                "clean_url": raw_url,
                "source": urlsplit(raw_url).netloc.lstrip("www."),
                "title": title,
                "description": "",
                "favicon_url": None,
                "extraction_source": "markdown_inline_links",
                "source_group": "primary",
                "is_more_source": False,
            }
        )
    return sources
