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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pyperclip
from selenium.common.exceptions import ElementClickInterceptedException, JavascriptException, NoSuchElementException, SessionNotCreatedException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium import webdriver
from selenium.webdriver import Chrome
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGGER = logging.getLogger(__name__)

CHAT_INPUT_SELECTORS = [
    "textarea[name='prompt-textarea']",
    "textarea[id='prompt-textarea']",
    "textarea[data-id='root']",
    "form textarea",
    "[contenteditable='true']",
]

SEND_BUTTON_SELECTORS = [
    "button[id='composer-submit-button'][data-testid='send-button']",
    "button[id='composer-submit-button']",
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "button[aria-label*='Send']",
    "button[type='submit']",
]

NEW_CHAT_SELECTORS = [
    "a[data-testid='create-new-chat-button']",
    "button[data-testid='create-new-chat-button']",
    "button[data-testid='new-chat-button']",
    "a[data-testid='new-chat-button']",
    "button[aria-label*='New chat']",
    "a[href='/']",
]

STOP_BUTTON_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop']",
    "button[aria-label*='stop']",
]

ASSISTANT_RESPONSE_SELECTOR = "div[data-message-author-role='assistant']"
CHAT_INPUT_SELECTOR = "textarea[id='prompt-textarea'], div[id='prompt-textarea'][contenteditable='true']"
SEND_BUTTON_SELECTOR = "button[data-testid='send-button']"
STOP_BUTTON_SELECTOR = "button[data-testid='stop-button']"
DISMISS_BUTTON_TEXT = {
    "accept",
    "agree",
    "continue",
    "dismiss",
    "got it",
    "not now",
    "ok",
    "okay",
    "skip",
    "start using chatgpt",
}


@dataclass
class ChatGPTCapture:
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


class ChatGPTRunner:
    def __init__(
        self,
        chatgpt_url: str,
        *,
        headless: bool = False,
        chrome_user_data_dir: str | None = None,
        login_wait_seconds: int = 180,
        response_timeout_seconds: int = 300,
        sources_panel_pause_seconds: int = 0,
        auto_login: bool = False,
        accounts: dict[str, dict[str, Any]] | None = None,
        login_email: str | None = None,
    ) -> None:
        self.chatgpt_url = chatgpt_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.login_wait_seconds = login_wait_seconds
        self.response_timeout_seconds = response_timeout_seconds
        self.sources_panel_pause_seconds = max(0, sources_panel_pause_seconds)
        self.auto_login = auto_login
        self.accounts = accounts or {}
        self.login_email = login_email
        self.driver: Chrome | None = None

    def __enter__(self) -> "ChatGPTRunner":
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
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.clipboard": 1,
                "profile.content_settings.exceptions.clipboard": {
                    "https://chatgpt.com:443,*": {"setting": 1},
                },
            },
        )
        if self.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
        if self.headless:
            options.add_argument("--headless=new")

        self.driver = self.create_driver(options)
        if not self.headless:
            vnc_screen = os.getenv("VNC_SCREEN", "1280x720x24")
            w, h = vnc_screen.split("x")[:2]
            self.driver.set_window_size(int(w), int(h))
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.get(self.chatgpt_url)
        self.recover_chrome_error_page(context="initial_chatgpt_load")
        if self.auto_login:
            self.run_automated_login()
        self.wait_for_login()

    def run_automated_login(self) -> None:
        from .chatgpt_auth import AutomatedLoginError, perform_automated_login

        if not self.login_email:
            raise RuntimeError("auto_login=True but no login_email was provided to ChatGPTRunner.")
        if not self.accounts:
            raise RuntimeError(
                "auto_login=True but no accounts were provided to ChatGPTRunner. "
                "Set CHATGPT_ACCOUNTS_B64 or pass accounts= when constructing the runner."
            )
        try:
            perform_automated_login(
                self.require_driver(),
                accounts=self.accounts,
                email=self.login_email,
                login_wait_seconds=self.login_wait_seconds,
            )
        except AutomatedLoginError as error:
            raise RuntimeError(f"Automated ChatGPT login failed: {error}") from error

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
            kwargs = {}
            if self.chrome_user_data_dir:
                kwargs["user_data_dir"] = self.chrome_user_data_dir
            chrome_major = detect_chrome_major_version()
            if chrome_major:
                LOGGER.info("Using undetected-chromedriver for local Chrome major version %s", chrome_major)
                kwargs["version_main"] = chrome_major
            return uc.Chrome(options=uc_options, **kwargs)
        except (ImportError, ModuleNotFoundError) as error:
            LOGGER.warning(
                "undetected-chromedriver is unavailable (%s). Falling back to Selenium Chrome. "
                "For best compatibility, use Python 3.12 or install a fixed undetected-chromedriver release when available.",
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
            self.driver.quit()
            self.driver = None

    def run_prompt(self, prompt_text: str) -> ChatGPTCapture:
        driver = self.require_driver()
        self.create_fresh_chat()
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
            raise RuntimeError("Captured response was empty or too short")
        if markdown:
            LOGGER.info("Markdown copied from ChatGPT response. length=%s method=%s", len(markdown.strip()), markdown_capture_method)
        else:
            LOGGER.warning("Markdown copy did not produce a valid clipboard result; markdown field will be empty. response_length=%s method=%s", len(response.strip()), markdown_capture_method)
        raw_html, raw_html_capture_method = self.capture_latest_response_html()
        LOGGER.info("Raw HTML extracted from ChatGPT response. length=%s method=%s", len(raw_html or ""), raw_html_capture_method)
        llm_model = self.capture_latest_response_model_slug() or "chatgpt"
        LOGGER.info("Detected ChatGPT response model. llm_model=%s", llm_model)
        sources = self.capture_latest_sources()
        source_capture_method = "sources_panel" if sources else "none"
        if not sources:
            LOGGER.info("No sources captured from Sources panel; trying markdown source fallback.")
            sources = extract_sources_from_markdown(response)
            source_capture_method = "markdown_references" if sources else "none"
        LOGGER.info("Captured %s source(s) using %s", len(sources), source_capture_method)
        return ChatGPTCapture(
            response=response.strip(),
            markdown=markdown.strip(),
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

    def wait_for_login(self) -> None:
        deadline = time.time() + self.login_wait_seconds
        while time.time() < deadline:
            self.recover_chrome_error_page(context="wait_for_login")
            if self.find_first(CHAT_INPUT_SELECTORS):
                return
            time.sleep(1)
        raise TimeoutError(
            "Timed out waiting for ChatGPT prompt input. Log in in the opened browser or set CHATGPT_CHROME_USER_DATA_DIR to a logged-in profile."
        )

    def recover_chrome_error_page(self, *, context: str, max_attempts: int = 2) -> bool:
        """
        Chrome can occasionally render its own HTTP error interstitial for chatgpt.com.
        If the visible page has Chrome's Reload button, click it and let the normal
        ChatGPT waits continue.
        """
        driver = self.require_driver()
        recovered = False
        for attempt in range(1, max_attempts + 1):
            state = self.chrome_error_page_state()
            if not state.get("is_error"):
                return recovered

            LOGGER.warning(
                "Detected Chrome error page during %s. attempt=%s/%s error_code=%s heading=%r current_url=%s",
                context,
                attempt,
                max_attempts,
                state.get("error_code") or "<unknown>",
                state.get("heading") or "",
                driver.current_url,
            )

            reload_button = self.find_first(["#reload-button", "button[data-url]", "button"])
            if reload_button and self.click_if_visible(reload_button):
                LOGGER.info("Clicked Chrome error page reload button during %s.", context)
            else:
                reload_url = str(state.get("reload_url") or "").strip()
                if reload_url:
                    LOGGER.info("Navigating to Chrome error page reload URL during %s: %s", context, reload_url)
                    driver.get(reload_url)
                else:
                    LOGGER.info("Refreshing Chrome error page during %s.", context)
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
                const isChatgptError = /chatgpt\\.com/i.test(document.title || location.href || '');
                return {
                  is_error: Boolean(isChromeNetError && isChatgptError),
                  error_code: errorCode || window.loadTimeDataRaw?.errorCode || '',
                  heading,
                  reload_url: reloadButton?.dataset?.url || window.loadTimeDataRaw?.reloadButton?.reloadUrl || '',
                  has_reload_button: Boolean(reloadButton),
                  body_class: bodyClass
                };
                """
            )
            return result if isinstance(result, dict) else {"is_error": False}
        except WebDriverException:
            return {"is_error": False}

    def create_fresh_chat(self) -> None:
        driver = self.require_driver()
        for selector in NEW_CHAT_SELECTORS:
            element = self.find_first([selector])
            if element and element.is_displayed():
                try:
                    element.click()
                    time.sleep(random.uniform(1.0, 2.5))
                    return
                except WebDriverException:
                    LOGGER.debug("Could not click new chat selector: %s", selector)
        driver.get(self.chatgpt_url)
        self.recover_chrome_error_page(context="create_fresh_chat")
        time.sleep(random.uniform(2.0, 4.0))

    def wait_for_input(self) -> WebElement:
        deadline = time.time() + 30
        while time.time() < deadline:
            self.dismiss_blocking_dialogs()
            element = self.wait_for_clickable_input(timeout=2)
            if element and element.is_displayed() and element.is_enabled():
                return element
            time.sleep(0.5)
        raise TimeoutException("Could not find ChatGPT input field")

    def type_prompt(self, input_element: WebElement, prompt_text: str) -> None:
        self.focus_input(input_element)
        select_modifier = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
        input_element.send_keys(select_modifier, "a")
        input_element.send_keys(Keys.BACKSPACE)

        # Type the first word character-by-character to trigger React's input detection,
        # then insert the remainder instantly to avoid the per-character VNC overhead.
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
                LOGGER.warning("Fast JS insert failed; falling back to character-by-character for remainder.")
                for char in remainder:
                    if char == "\n":
                        input_element.send_keys(Keys.SHIFT, Keys.ENTER)
                    else:
                        input_element.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))

        time.sleep(0.15)

    def _js_insert_at_cursor(self, input_element: WebElement, text: str) -> bool:
        """Insert text at the current cursor position using execCommand.

        execCommand('insertText') works for both contenteditable divs and textareas
        in Chrome, fires the correct InputEvent that React picks up, and respects
        the current cursor position so previously-typed characters are preserved.
        """
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
        button = self.wait_for_clickable(By.CSS_SELECTOR, SEND_BUTTON_SELECTOR, timeout=10)
        if button:
            self.click_element(button)
            return

        for selector in SEND_BUTTON_SELECTORS:
            button = self.find_first([selector])
            if button and button.is_displayed() and button.is_enabled():
                self.click_element(button)
                return
        input_element.send_keys(Keys.ENTER)

    def focus_input(self, input_element: WebElement) -> None:
        driver = self.require_driver()
        self.dismiss_blocking_dialogs()
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", input_element)
        time.sleep(0.2)
        try:
            input_element.click()
            return
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
            label = " ".join(
                value.strip()
                for value in [
                    button.text or "",
                    button.get_attribute("aria-label") or "",
                    button.get_attribute("title") or "",
                ]
                if value and value.strip()
            ).strip().lower()
            if label in DISMISS_BUTTON_TEXT and self.click_if_visible(button):
                time.sleep(0.5)
                return

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

    def wait_for_clickable_input(self, timeout: int = 10) -> WebElement | None:
        return self.wait_for_clickable(By.CSS_SELECTOR, CHAT_INPUT_SELECTOR, timeout=timeout)

    def wait_for_clickable(self, by: str, selector: str, timeout: int = 10) -> WebElement | None:
        try:
            return WebDriverWait(self.require_driver(), timeout).until(EC.element_to_be_clickable((by, selector)))
        except TimeoutException:
            return None

    def wait_for_response_completion(self, initial_response_count: int) -> None:
        stop_button_seen = self.wait_for_stop_button(timeout=30)
        if stop_button_seen:
            if self.wait_for_stop_button_to_disappear(timeout=self.response_timeout_seconds):
                time.sleep(2)
                return
            raise TimeoutError(f"ChatGPT stop button did not disappear. {self.collect_page_signals()}")

        LOGGER.warning("ChatGPT stop button did not appear after submit. Falling back to response stability wait. %s", self.collect_page_signals())

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

        raise TimeoutError("Timed out waiting for ChatGPT response to complete")

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

        keywords = [
            "something went wrong",
            "unusual activity",
            "verify",
            "captcha",
            "network error",
            "try again",
            "unable to load",
            "temporary chat",
            "limit",
        ]
        lowered = body_text.lower()
        matches = [keyword for keyword in keywords if keyword in lowered]
        if matches:
            return f"Visible page signals: {', '.join(matches)}"
        return "No obvious blocking page text detected."

    def capture_latest_response(self) -> tuple[str, str, str]:
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "", "no_response_element"

        visible_text = latest_response.text or ""
        parent = self.latest_response_turn_element(latest_response) or self.response_action_container(latest_response)
        copy_button = self.find_copy_button(parent) if parent else None
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
                        if self.is_suspicious_copied_response(copied, visible_text):
                            LOGGER.warning(
                                "Ignoring suspicious copied markdown. attempt=%s copied_length=%s visible_text_length=%s copied_preview=%r",
                                attempt,
                                len(copied),
                                len(visible_text),
                                copied[:200],
                            )
                            break
                        return visible_text, copied, f"copy_button_markdown_attempt_{attempt}"
                    time.sleep(0.2)
                LOGGER.info("Markdown copy attempt did not produce a valid clipboard payload. attempt=%s/3", attempt)
                time.sleep(1)
            except WebDriverException as exc:
                LOGGER.warning("Markdown copy attempt failed. attempt=%s/3 error=%s", attempt, first_line(str(exc)))
                time.sleep(1)

        return visible_text, "", "copy_button_markdown_failed_after_retries"

    def is_suspicious_copied_response(self, copied: str, visible_text: str) -> bool:
        copied_lines = [line.strip() for line in copied.splitlines() if line.strip()]
        if len(copied_lines) < 6:
            return False

        sourceish_lines = 0
        for line in copied_lines:
            words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.'-]*", line)
            if line.startswith("+") or len(words) <= 3:
                sourceish_lines += 1

        sourceish_ratio = sourceish_lines / max(1, len(copied_lines))
        visible_words = set(normalize_words(visible_text))
        copied_words = set(normalize_words(copied))
        overlap_ratio = len(visible_words & copied_words) / max(1, min(len(visible_words), len(copied_words)))

        return sourceish_ratio >= 0.75 and overlap_ratio < 0.45

    def capture_latest_response_html(self) -> tuple[str, str]:
        latest_response = self.latest_response_element()
        if not latest_response:
            return "", "none"

        html = self.element_outer_html(latest_response)
        if html.strip():
            return html, "assistant_message_outer_html"

        turn = self.latest_response_turn_element(latest_response)
        if turn:
            html = self.element_outer_html(turn)
            if html.strip():
                return html, "assistant_turn_outer_html_fallback"

        return "", "none"

    def capture_latest_response_model_slug(self) -> str:
        latest_response = self.latest_response_element()
        if not latest_response:
            return ""

        model_slug = (latest_response.get_attribute("data-message-model-slug") or "").strip()
        if model_slug:
            return model_slug

        try:
            nested_model_slug = self.require_driver().execute_script(
                """
                const response = arguments[0];
                return response.querySelector('[data-message-model-slug]')?.getAttribute('data-message-model-slug') || '';
                """,
                latest_response,
            )
            if nested_model_slug:
                return str(nested_model_slug).strip()
        except WebDriverException:
            pass

        turn = self.latest_response_turn_element(latest_response)
        if turn:
            try:
                turn_model_slug = self.require_driver().execute_script(
                    """
                    const turn = arguments[0];
                    return turn.querySelector('[data-message-model-slug]')?.getAttribute('data-message-model-slug') || '';
                    """,
                    turn,
                )
                return str(turn_model_slug or "").strip()
            except WebDriverException:
                return ""

        return ""

    def element_outer_html(self, element: WebElement) -> str:
        try:
            html = self.require_driver().execute_script("return arguments[0].outerHTML || '';", element)
            return str(html or "")
        except WebDriverException:
            return ""

    def capture_latest_sources(self) -> list[dict[str, Any]]:
        latest_response = self.latest_response_element()
        if not latest_response:
            LOGGER.info("Skipping source capture: no latest assistant response element found.")
            return []

        parent = self.response_action_container(latest_response)
        if not parent:
            LOGGER.info("Skipping source capture: could not locate response action container.")
            return []

        self.reveal_response_actions(latest_response)
        sources_button = self.find_sources_button(parent)
        if not sources_button:
            LOGGER.info("No Sources button found for latest ChatGPT response. %s", self.sources_button_diagnostics(parent))
            return []

        LOGGER.info("Sources button found; opening Sources panel.")
        self.click_element(sources_button)
        self.pause_after_sources_panel_open()
        if not self.wait_for_sources_panel_root(timeout=10):
            LOGGER.warning("Sources panel did not appear after clicking Sources. %s", self.sources_panel_diagnostics())
            return []
        LOGGER.info("Sources panel opened. %s", self.sources_panel_diagnostics())
        if not self.wait_for_sources_panel_links(timeout=30):
            LOGGER.warning("Sources panel opened but no source links were detected. %s", self.sources_panel_diagnostics())
            self.close_sources_panel()
            return []
        LOGGER.info("Sources panel links loaded. %s", self.sources_panel_diagnostics())

        try:
            self.scroll_sources_panel_to_end()
            sources = self.extract_sources_from_panel()
            if not sources:
                LOGGER.warning("Sources panel was found, but extraction returned 0 sources. %s", self.sources_panel_diagnostics())
            else:
                LOGGER.info("Sources copied from panel. count=%s", len(sources))
            return sources
        finally:
            self.close_sources_panel()

    def find_sources_button(self, container: WebElement) -> WebElement | None:
        buttons = container.find_elements(By.CSS_SELECTOR, "button")
        for button in reversed(buttons):
            label = " ".join(
                value.strip()
                for value in [
                    button.get_attribute("aria-label") or "",
                    button.text or "",
                    button.get_attribute("title") or "",
                ]
                if value and value.strip()
            ).strip().lower()
            if label == "sources" or label.endswith(" sources") or "sources" in label:
                if button.is_displayed() and button.is_enabled():
                    return button
        return None

    def reveal_response_actions(self, response: WebElement) -> None:
        try:
            ActionChains(self.require_driver()).move_to_element(response).perform()
            time.sleep(0.3)
        except WebDriverException:
            pass

    def pause_after_sources_panel_open(self) -> None:
        if self.sources_panel_pause_seconds <= 0:
            return
        LOGGER.info(
            "Sources panel debug pause active for %s seconds. Inspect/copy the DOM now; automation will continue afterwards.",
            self.sources_panel_pause_seconds,
        )
        time.sleep(self.sources_panel_pause_seconds)

    def wait_for_sources_panel_root(self, timeout: int = 10) -> bool:
        self.install_sources_panel_helpers()
        try:
            WebDriverWait(self.require_driver(), timeout).until(
                lambda driver: driver.execute_script(
                    """
                    const root = window.__brandsightFindSourcesPanel?.() || null;
                    return Boolean(root || window.__brandsightHasSourcesPanelMarkers?.());
                    """
                )
            )
            return True
        except TimeoutException:
            return False

    def wait_for_sources_panel_links(self, timeout: int = 30) -> bool:
        self.install_sources_panel_helpers()
        deadline = time.time() + timeout
        last_count = -1
        stable_checks = 0
        last_logged_second = -1
        while time.time() < deadline:
            count = self.require_driver().execute_script(
                """
                return window.__brandsightCollectSourceLinks?.().length || 0;
                """
            )
            elapsed = int(timeout - max(0, deadline - time.time()))
            if count != last_count or elapsed >= last_logged_second + 5:
                LOGGER.info("Waiting for Sources panel links. visible_link_count=%s stable_checks=%s elapsed=%ss", count, stable_checks, elapsed)
                last_logged_second = elapsed
            if count > 0 and count == last_count:
                stable_checks += 1
            elif count > 0:
                stable_checks = 0
                last_count = count
            if stable_checks >= 2:
                return True
            time.sleep(0.75)
        return False

    def scroll_sources_panel_to_end(self) -> None:
        driver = self.require_driver()
        self.install_sources_panel_helpers()
        last_count = -1
        stable_count = 0
        for _ in range(8):
            count = driver.execute_script(
                """
                const root = window.__brandsightFindSourcesPanel?.() || null;
                if (root) {
                  const scrollables = [root, ...root.querySelectorAll('*')]
                  .filter(el => el.scrollHeight > el.clientHeight + 20);
                  for (const el of scrollables) el.scrollTop = el.scrollHeight;
                }
                return window.__brandsightCollectSourceLinks?.().length || 0;
                """
            )
            if count == last_count:
                stable_count += 1
            else:
                stable_count = 0
                last_count = count
            if stable_count >= 2:
                return
            time.sleep(0.5)

    def extract_sources_from_panel(self) -> list[dict[str, Any]]:
        self.install_sources_panel_helpers()
        raw_sources = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindSourcesPanel?.() || null;
            const links = window.__brandsightCollectSourceLinks?.() || [];
            const isUnderMoreHeading = (link) => {
              const stopNode = root || document.body;
              let node = link;
              while (node && node !== stopNode) {
                let sibling = node.previousElementSibling;
                while (sibling) {
                  const text = (sibling.innerText || '').trim().toLowerCase();
                  if (text === 'more') return true;
                  sibling = sibling.previousElementSibling;
                }
                node = node.parentElement;
              }
              return false;
            };
            return links.map((link, index) => {
              const childTexts = [...link.children]
                .map((child) => (child.innerText || '').trim())
                .filter(Boolean);
              const allLines = (link.innerText || '')
                .split('\\n')
                .map((line) => line.trim())
                .filter(Boolean);
              const source = childTexts[0] || allLines[0] || '';
              const title = childTexts[1] || allLines[1] || '';
              const description = childTexts.slice(2).join('\\n') || allLines.slice(2).join('\\n') || '';
              const favicon = link.querySelector('img')?.src || '';
              const isMoreSource = isUnderMoreHeading(link);
              return {
                index: index + 1,
                url: link.href,
                source,
                title,
                description,
                favicon_url: favicon,
                source_group: isMoreSource ? 'more' : 'primary',
                is_more_source: isMoreSource
              };
            });
            """
        )
        if not isinstance(raw_sources, list):
            return []

        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            raw_url = str(source.get("url") or "").strip()
            if not raw_url or raw_url in seen_urls:
                continue
            seen_urls.add(raw_url)
            sources.append(
                {
                    "index": len(sources) + 1,
                    "url": raw_url,
                    "clean_url": clean_chatgpt_source_url(raw_url),
                    "source": clean_text(source.get("source")),
                    "title": clean_text(source.get("title")),
                    "description": clean_text(source.get("description")),
                    "favicon_url": str(source.get("favicon_url") or "").strip() or None,
                    "extraction_source": "sources_panel",
                    "source_group": clean_text(source.get("source_group")) or "primary",
                    "is_more_source": bool(source.get("is_more_source")),
                }
            )
        return sources

    def close_sources_panel(self) -> None:
        driver = self.require_driver()
        close_button = self.find_first([
            "[data-testid='screen-threadFlyOut'][aria-label='Sources'] button[aria-label='Close']",
            "[data-testid='stage-thread-flyout'] section[aria-label='Sources'] button[aria-label='Close']",
            "section[aria-label='Sources'] button[aria-label='Close']",
            "[data-testid='bar-search-sources-header'] button[aria-label='Close']",
            "#modal-search-results button[data-testid='close-button']",
            "[data-testid='modal-search-results'] button[aria-label='Close']",
            "[role='dialog'] button[data-testid='close-button']",
            "[role='dialog'] button[aria-label='Close']",
        ])
        if close_button:
            self.click_if_visible(close_button)
            time.sleep(0.5)
            return
        try:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except WebDriverException:
            pass

    def install_sources_panel_helpers(self) -> None:
        self.require_driver().execute_script(
            """
            window.__brandsightFindSourcesPanel = function () {
              const sourceSectionSelectors = [
                'section[aria-label="Sources"][data-testid="screen-threadFlyOut"]',
                '[data-testid="stage-thread-flyout"] section[aria-label="Sources"]',
                '[data-testid="screen-threadFlyOut"][aria-label="Sources"]',
                '[data-testid="screen-threadFlyOut"]',
                '[data-testid="stage-thread-flyout"]',
                '[data-primary-scroller="true"][aria-label="Sources"]',
                'section[aria-label="Sources"]'
              ];
              for (const selector of sourceSectionSelectors) {
                for (const candidate of document.querySelectorAll(selector)) {
                  const text = (candidate.innerText || '').trim().toLowerCase();
                  if (candidate.getAttribute('aria-label') === 'Sources' || text.startsWith('sources') || text.includes('\\nmore\\n')) {
                    return candidate;
                  }
                }
              }

              const sourcesHeader = document.querySelector('[data-testid="bar-search-sources-header"]');
              if (sourcesHeader) {
                const flyout = sourcesHeader.closest('[data-testid="screen-threadFlyOut"], section[aria-label="Sources"], [data-primary-scroller="true"], [data-testid="stage-thread-flyout"]');
                if (flyout) return flyout;
              }

              const explicit = document.querySelector('#modal-search-results, [data-testid="modal-search-results"]');
              if (explicit) return explicit;

              const dialogs = [...document.querySelectorAll('[role="dialog"], .popover')];
              const sourcesDialog = dialogs.find((dialog) => {
                const text = (dialog.innerText || '').trim().toLowerCase();
                return text.startsWith('sources') || text.includes('\\nsources\\n') || text.includes('sources');
              });
              if (sourcesDialog) return sourcesDialog;

              const headings = [...document.querySelectorAll('h1,h2,h3,[aria-labelledby]')]
                .filter((node) => (node.innerText || '').trim().toLowerCase() === 'sources');
              for (const heading of headings) {
                const dialog = heading.closest('[role="dialog"], .popover, [data-state="open"]');
                if (dialog) return dialog;
              }

              return null;
            };

            window.__brandsightHasSourcesPanelMarkers = function () {
              return Boolean(
                document.querySelector('[data-testid="bar-search-sources-header"]') ||
                document.querySelector('[data-testid="screen-threadFlyOut"][aria-label="Sources"]') ||
                document.querySelector('[data-testid="stage-thread-flyout"] section[aria-label="Sources"]') ||
                document.querySelector('section[aria-label="Sources"]')
              );
            };

            window.__brandsightCollectSourceLinks = function () {
              const root = window.__brandsightFindSourcesPanel?.() || null;
              const rootLinks = root
                ? [...root.querySelectorAll('a[href]')].filter((link) => /^https?:\\/\\//.test(link.href))
                : [];
              if (root) return rootLinks;

              if (!window.__brandsightHasSourcesPanelMarkers?.()) return [];
              return [...document.querySelectorAll('a[href*="utm_source=chatgpt.com"]')]
                .filter((link) => /^https?:\\/\\//.test(link.href));
            };
            """
        )

    def sources_panel_diagnostics(self) -> str:
        self.install_sources_panel_helpers()
        result = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindSourcesPanel?.() || null;
            const modalCount = document.querySelectorAll('#modal-search-results, [data-testid="modal-search-results"]').length;
            const dialogCount = document.querySelectorAll('[role="dialog"], .popover').length;
            const stageThreadFlyoutCount = document.querySelectorAll('[data-testid="stage-thread-flyout"]').length;
            const screenThreadFlyoutCount = document.querySelectorAll('[data-testid="screen-threadFlyOut"]').length;
            const sourcesSectionCount = document.querySelectorAll('section[aria-label="Sources"], [data-testid="screen-threadFlyOut"][aria-label="Sources"]').length;
            const sourcesHeaderCount = document.querySelectorAll('[data-testid="bar-search-sources-header"]').length;
            const fallbackUtmLinkCount = document.querySelectorAll('a[href*="utm_source=chatgpt.com"]').length;
            const collectedLinks = window.__brandsightCollectSourceLinks?.().map((link) => link.href) || [];
            if (!root) {
              return {
                found: false,
                modalCount,
                dialogCount,
                stageThreadFlyoutCount,
                screenThreadFlyoutCount,
                sourcesSectionCount,
                sourcesHeaderCount,
                fallbackUtmLinkCount,
                collectedLinkCount: collectedLinks.length,
                sampleCollectedLinks: collectedLinks.slice(0, 5),
                linkCount: 0,
                textPreview: ''
              };
            }
            const links = [...root.querySelectorAll('a[href]')].map((link) => link.href);
            return {
              found: true,
              modalCount,
              dialogCount,
              stageThreadFlyoutCount,
              screenThreadFlyoutCount,
              sourcesSectionCount,
              sourcesHeaderCount,
              fallbackUtmLinkCount,
              linkCount: links.length,
              externalLinkCount: links.filter((href) => /^https?:\\/\\//.test(href)).length,
              collectedLinkCount: collectedLinks.length,
              sampleLinks: links.slice(0, 5),
              sampleCollectedLinks: collectedLinks.slice(0, 5),
              textPreview: (root.innerText || '').slice(0, 500)
            };
            """
        )
        return str(result)

    def sources_button_diagnostics(self, container: WebElement) -> str:
        try:
            result = self.require_driver().execute_script(
                """
                const container = arguments[0];
                const buttons = [...container.querySelectorAll('button')];
                return {
                  buttonCount: buttons.length,
                  buttonLabels: buttons.slice(-12).map((button) => ({
                    text: (button.innerText || '').trim(),
                    ariaLabel: button.getAttribute('aria-label') || '',
                    title: button.getAttribute('title') || '',
                    testId: button.getAttribute('data-testid') || ''
                  }))
                };
                """,
                container,
            )
            return str(result)
        except WebDriverException:
            return "No button diagnostics available."

    def capture_product_flyouts(self) -> list[dict[str, Any]]:
        latest_response = self.latest_response_element()
        if not latest_response:
            LOGGER.info("Skipping product capture: no latest assistant response element found.")
            return []

        scope = self.latest_response_turn_element(latest_response) or latest_response
        button_count = self.count_product_buttons(scope)
        if button_count <= 0:
            LOGGER.info("No product select buttons found for latest ChatGPT response.")
            return []

        LOGGER.info("Found %s product select button(s); opening product flyouts. %s", button_count, self.product_button_diagnostics(scope))
        products: list[dict[str, Any]] = []
        for index in range(button_count):
            self.close_product_flyout()
            LOGGER.info("Opening product flyout. product_index=%s/%s", index + 1, button_count)
            if not self.click_product_button_by_index(scope, index):
                LOGGER.warning("Could not click product button. product_index=%s/%s", index + 1, button_count)
                products.append(
                    {
                        "index": index + 1,
                        "button_index": index + 1,
                        "capture_method": "product_button_click_failed",
                        "raw_html": "",
                        "html_length": 0,
                        "title": "",
                    }
                )
                continue

            if not self.wait_for_product_flyout_root(timeout=10):
                LOGGER.warning(
                    "Product flyout did not appear. product_index=%s/%s %s",
                    index + 1,
                    button_count,
                    self.product_flyout_diagnostics(),
                )
                products.append(
                    {
                        "index": index + 1,
                        "button_index": index + 1,
                        "capture_method": "product_flyout_not_found",
                        "raw_html": "",
                        "html_length": 0,
                        "title": "",
                    }
                )
                continue

            if not self.wait_for_product_flyout_content(timeout=45):
                LOGGER.warning(
                    "Product flyout opened but content did not stabilize. product_index=%s/%s %s",
                    index + 1,
                    button_count,
                    self.product_flyout_diagnostics(),
                )

            product = self.extract_product_flyout(index + 1)
            LOGGER.info(
                "Product flyout captured. product_index=%s/%s title=%r html_length=%s",
                index + 1,
                button_count,
                product.get("title") or "",
                product.get("html_length") or 0,
            )
            products.append(product)
            self.close_product_flyout()

        return products

    def count_product_buttons(self, scope: WebElement) -> int:
        self.install_product_flyout_helpers()
        try:
            count = self.require_driver().execute_script(
                """
                return window.__brandsightCollectProductOpeners?.(arguments[0]).length || 0;
                """,
                scope,
            )
            return int(count or 0)
        except (WebDriverException, ValueError, TypeError):
            return 0

    def click_product_button_by_index(self, scope: WebElement, index: int) -> bool:
        self.install_product_flyout_helpers()
        try:
            return bool(
                self.require_driver().execute_script(
                    """
                    return window.__brandsightClickProductOpener?.(arguments[0], arguments[1]) || false;
                    """,
                    scope,
                    index,
                )
            )
        except WebDriverException:
            return False

    def wait_for_product_flyout_root(self, timeout: int = 10) -> bool:
        self.install_product_flyout_helpers()
        try:
            WebDriverWait(self.require_driver(), timeout).until(
                lambda driver: driver.execute_script(
                    """
                    return Boolean(window.__brandsightFindProductFlyout?.());
                    """
                )
            )
            return True
        except TimeoutException:
            return False

    def wait_for_product_flyout_content(self, timeout: int = 45) -> bool:
        self.install_product_flyout_helpers()
        deadline = time.time() + timeout
        last_signature = ""
        stable_checks = 0
        last_logged_second = -1
        while time.time() < deadline:
            state = self.require_driver().execute_script(
                """
                const root = window.__brandsightFindProductFlyout?.();
                if (!root) {
                  return {
                    ready: false,
                    loading: false,
                    signature: '',
                    textLength: 0,
                    imageCount: 0,
                    linkCount: 0,
                    markerCount: 0,
                    textPreview: ''
                  };
                }
                const text = (root.innerText || '').trim();
                const normalized = text.toLowerCase();
                const imageCount = root.querySelectorAll('img[src]').length;
                const linkCount = root.querySelectorAll('a[href]').length;
                const loading = normalized.includes('looking up product details');
                const markerCount = [
                  '[data-testid="bar-product-header"]',
                  'section[aria-label="Product details"] h2',
                  'button[aria-label^="Open offer from"]',
                  'button[aria-label^="Open "]',
                  'img[src*="images.openai.com"]'
                ].reduce((count, selector) => count + root.querySelectorAll(selector).length, 0);
                const textMarkers = [
                  'what to know',
                  'what people are saying',
                  'in stock',
                  'visit',
                  'explore more',
                  'delivery',
                  'reviews'
                ].filter((marker) => normalized.includes(marker)).length;
                const ready = !loading && text.length >= 80 && (markerCount + textMarkers + imageCount + linkCount) > 0;
                return {
                  ready,
                  loading,
                  signature: `${text.length}:${imageCount}:${linkCount}:${markerCount}:${textMarkers}:${text.slice(0, 120)}`,
                  textLength: text.length,
                  imageCount,
                  linkCount,
                  markerCount: markerCount + textMarkers,
                  textPreview: text.slice(0, 160)
                };
                """
            )
            if not isinstance(state, dict):
                state = {}

            elapsed = int(timeout - max(0, deadline - time.time()))
            if elapsed >= last_logged_second + 5:
                LOGGER.info(
                    "Waiting for product flyout content. ready=%s loading=%s text_length=%s image_count=%s link_count=%s marker_count=%s elapsed=%ss preview=%r",
                    state.get("ready"),
                    state.get("loading"),
                    state.get("textLength"),
                    state.get("imageCount"),
                    state.get("linkCount"),
                    state.get("markerCount"),
                    elapsed,
                    state.get("textPreview") or "",
                )
                last_logged_second = elapsed

            signature = str(state.get("signature") or "")
            if state.get("ready") and signature and signature == last_signature:
                stable_checks += 1
            elif state.get("ready") and signature:
                stable_checks = 0
                last_signature = signature
            else:
                stable_checks = 0
                last_signature = signature
            if stable_checks >= 2:
                return True
            time.sleep(0.75)
        return False

    def extract_product_flyout(self, index: int) -> dict[str, Any]:
        self.install_product_flyout_helpers()
        raw = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindProductFlyout?.();
            if (!root) return null;
            const section = root.matches?.('section[aria-label="Product details"]')
              ? root
              : root.querySelector('section[aria-label="Product details"]') || root;
            const title = (
              section.querySelector('h1,h2,h3,[data-testid*="title"]')?.innerText ||
              section.querySelector(
                'button[aria-label]:not([aria-label="Close"]):not([aria-label="Select product"])'
              )?.getAttribute('aria-label') ||
              ''
            ).trim();
            const links = [...section.querySelectorAll('a[href]')].map((link, linkIndex) => ({
              index: linkIndex + 1,
              url: link.href,
              text: (link.innerText || '').trim()
            }));
            const images = [...section.querySelectorAll('img[src]')].map((image, imageIndex) => ({
              index: imageIndex + 1,
              src: image.src,
              alt: image.alt || ''
            }));
            return {
              raw_html: root.outerHTML || '',
              title,
              text_length: (section.innerText || '').trim().length,
              link_count: links.length,
              image_count: images.length,
              links,
              images
            };
            """
        )
        if not isinstance(raw, dict):
            return {
                "index": index,
                "button_index": index,
                "capture_method": "product_flyout_outer_html",
                "raw_html": "",
                "html_length": 0,
                "title": "",
                "text_length": 0,
                "link_count": 0,
                "image_count": 0,
                "links": [],
                "images": [],
            }

        html = str(raw.get("raw_html") or "")
        return {
            "index": index,
            "button_index": index,
            "capture_method": "product_flyout_outer_html",
            "raw_html": html,
            "html_length": len(html),
            "title": clean_text(raw.get("title")),
            "text_length": int(raw.get("text_length") or 0),
            "link_count": int(raw.get("link_count") or 0),
            "image_count": int(raw.get("image_count") or 0),
            "links": raw.get("links") if isinstance(raw.get("links"), list) else [],
            "images": raw.get("images") if isinstance(raw.get("images"), list) else [],
        }

    def close_product_flyout(self) -> None:
        driver = self.require_driver()
        self.install_product_flyout_helpers()
        try:
            closed = driver.execute_script(
                """
                const root = window.__brandsightFindProductFlyout?.();
                const closeButton = root?.querySelector('button[aria-label="Close"]');
                if (!closeButton) return false;
                closeButton.click();
                return true;
                """
            )
            if closed:
                time.sleep(0.5)
                return
        except WebDriverException:
            pass

        close_button = self.find_first(["section[aria-label='Product details'] button[aria-label='Close']"])
        if close_button and self.click_if_visible(close_button):
            time.sleep(0.5)
            return
        try:
            if driver.execute_script("return Boolean(window.__brandsightFindProductFlyout?.());"):
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                time.sleep(0.5)
        except WebDriverException:
            pass

    def install_product_flyout_helpers(self) -> None:
        self.require_driver().execute_script(
            """
            window.__brandsightCollectProductOpeners = function (root) {
              const scope = root || document;
              const buttons = [...scope.querySelectorAll('button[aria-label="Select product"]')]
                .filter((button) => button.isConnected && button.getClientRects().length > 0);

              return buttons.map((button, index) => {
                const roleButton = button.closest('[role="button"]');
                const imageTarget = button.closest('[data-shopping-product-image-pdp-click-target="true"]');
                const cardButton = button.closest('button');
                const target = roleButton || imageTarget || cardButton || button;
                const card = target.closest('[role="button"], button, th, div') || target;
                const title = (
                  card.querySelector('h1,h2,h3,[aria-label],.font-semibold,.line-clamp-2')?.innerText ||
                  card.querySelector('img[alt]')?.alt ||
                  target.getAttribute('aria-label') ||
                  button.getAttribute('aria-label') ||
                  ''
                ).trim();
                return {
                  index,
                  button,
                  target,
                  title,
                  targetTag: target.tagName,
                  targetRole: target.getAttribute('role') || '',
                  targetTextPreview: (target.innerText || '').trim().slice(0, 120)
                };
              });
            };

            window.__brandsightDispatchProductClick = function (target) {
              target.scrollIntoView({block: 'center', inline: 'center'});
              const rect = target.getBoundingClientRect();
              const options = {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: rect.left + Math.max(1, rect.width / 2),
                clientY: rect.top + Math.max(1, rect.height / 2),
                pointerType: 'mouse'
              };
              target.dispatchEvent(new PointerEvent('pointerover', options));
              target.dispatchEvent(new MouseEvent('mouseover', options));
              target.dispatchEvent(new PointerEvent('pointermove', options));
              target.dispatchEvent(new MouseEvent('mousemove', options));
              target.dispatchEvent(new PointerEvent('pointerdown', options));
              target.dispatchEvent(new MouseEvent('mousedown', options));
              target.dispatchEvent(new PointerEvent('pointerup', options));
              target.dispatchEvent(new MouseEvent('mouseup', options));
              target.dispatchEvent(new MouseEvent('click', options));
              return true;
            };

            window.__brandsightClickProductOpener = function (root, index) {
              const candidate = window.__brandsightCollectProductOpeners(root)[index];
              if (!candidate?.target) return false;
              return window.__brandsightDispatchProductClick(candidate.target);
            };

            window.__brandsightFindProductFlyout = function () {
              const productSectionSelectors = [
                'section[aria-label="Product details"][data-testid="screen-threadFlyOut"]',
                '[data-testid="stage-thread-flyout"] section[aria-label="Product details"]',
                '[data-testid="stage-thread-flyout"] [data-testid="bar-product-header"]',
                '[data-testid="screen-threadFlyOut"] [data-testid="bar-product-header"]',
                'section[aria-label="Product details"]'
              ];
              for (const selector of productSectionSelectors) {
                const marker = document.querySelector(selector);
                if (marker) return marker.closest('[data-testid="stage-thread-flyout"]') || marker.closest('[data-testid="screen-threadFlyOut"]') || marker;
              }

              const flyouts = [
                ...document.querySelectorAll('[data-testid="stage-thread-flyout"], [data-testid="screen-threadFlyOut"]')
              ];
              return flyouts.find((flyout) => {
                const text = (flyout.innerText || '').trim().toLowerCase();
                return (
                  flyout.querySelector('[data-testid="bar-product-header"]') ||
                  text.includes('product details') ||
                  text.includes('what to know') ||
                  text.includes('what people are saying') ||
                  text.includes('buying options')
                );
              }) || null;
            };
            """
        )

    def product_button_diagnostics(self, scope: WebElement) -> str:
        self.install_product_flyout_helpers()
        try:
            result = self.require_driver().execute_script(
                """
                const candidates = window.__brandsightCollectProductOpeners?.(arguments[0]) || [];
                return {
                  candidateCount: candidates.length,
                  candidates: candidates.slice(0, 10).map((candidate) => ({
                    index: candidate.index + 1,
                    title: candidate.title,
                    targetTag: candidate.targetTag,
                    targetRole: candidate.targetRole,
                    targetTextPreview: candidate.targetTextPreview
                  }))
                };
                """,
                scope,
            )
            return str(result)
        except WebDriverException:
            return "No product button diagnostics available."

    def product_flyout_diagnostics(self) -> str:
        self.install_product_flyout_helpers()
        result = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindProductFlyout?.() || null;
            return {
              found: Boolean(root),
              stageThreadFlyoutCount: document.querySelectorAll('[data-testid="stage-thread-flyout"]').length,
              screenThreadFlyoutCount: document.querySelectorAll('[data-testid="screen-threadFlyOut"]').length,
              productHeaderCount: document.querySelectorAll('[data-testid="bar-product-header"]').length,
              productSectionCount: document.querySelectorAll('section[aria-label="Product details"]').length,
              selectProductButtonCount: document.querySelectorAll('button[aria-label="Select product"]').length,
              textPreview: root ? (root.innerText || '').slice(0, 500) : ''
            };
            """
        )
        return str(result)

    def capture_entity_flyouts(self) -> list[dict[str, Any]]:
        latest_response = self.latest_response_element()
        if not latest_response:
            LOGGER.info("Skipping entity capture: no latest assistant response element found.")
            return []

        scope = self.latest_response_turn_element(latest_response) or latest_response
        entity_count = self.count_entity_openers(scope)
        if entity_count <= 0:
            LOGGER.info("No entity underline elements found for latest ChatGPT response.")
            return []

        LOGGER.info("Found %s entity element(s); opening entity flyouts. %s", entity_count, self.entity_button_diagnostics(scope))
        entities: list[dict[str, Any]] = []
        for index in range(entity_count):
            self.close_entity_flyout()
            LOGGER.info("Opening entity flyout. entity_index=%s/%s", index + 1, entity_count)
            if not self.click_entity_by_index(scope, index):
                LOGGER.warning("Could not click entity element. entity_index=%s/%s", index + 1, entity_count)
                entities.append(
                    {
                        "index": index + 1,
                        "entity_index": index + 1,
                        "capture_method": "entity_click_failed",
                        "raw_html": "",
                        "html_length": 0,
                        "title": "",
                        "entity_text": "",
                    }
                )
                continue

            if not self.wait_for_entity_flyout_root(timeout=10):
                LOGGER.warning(
                    "Entity flyout did not appear. entity_index=%s/%s %s",
                    index + 1,
                    entity_count,
                    self.entity_flyout_diagnostics(),
                )
                entities.append(
                    {
                        "index": index + 1,
                        "entity_index": index + 1,
                        "capture_method": "entity_flyout_not_found",
                        "raw_html": "",
                        "html_length": 0,
                        "title": "",
                        "entity_text": "",
                    }
                )
                continue

            if not self.wait_for_entity_flyout_content(timeout=30):
                LOGGER.warning(
                    "Entity flyout opened but content did not stabilize. entity_index=%s/%s %s",
                    index + 1,
                    entity_count,
                    self.entity_flyout_diagnostics(),
                )

            entity = self.extract_entity_flyout(index + 1)
            LOGGER.info(
                "Entity flyout captured. entity_index=%s/%s entity_text=%r title=%r html_length=%s",
                index + 1,
                entity_count,
                entity.get("entity_text") or "",
                entity.get("title") or "",
                entity.get("html_length") or 0,
            )
            entities.append(entity)
            self.close_entity_flyout()

        return entities

    def count_entity_openers(self, scope: WebElement) -> int:
        self.install_entity_flyout_helpers()
        try:
            count = self.require_driver().execute_script(
                """
                return window.__brandsightCollectEntityOpeners?.(arguments[0]).length || 0;
                """,
                scope,
            )
            return int(count or 0)
        except (WebDriverException, ValueError, TypeError):
            return 0

    def click_entity_by_index(self, scope: WebElement, index: int) -> bool:
        self.install_entity_flyout_helpers()
        try:
            return bool(
                self.require_driver().execute_script(
                    """
                    return window.__brandsightClickEntityOpener?.(arguments[0], arguments[1]) || false;
                    """,
                    scope,
                    index,
                )
            )
        except WebDriverException:
            return False

    def wait_for_entity_flyout_root(self, timeout: int = 10) -> bool:
        self.install_entity_flyout_helpers()
        try:
            WebDriverWait(self.require_driver(), timeout).until(
                lambda driver: driver.execute_script(
                    """
                    return Boolean(window.__brandsightFindEntityFlyout?.());
                    """
                )
            )
            return True
        except TimeoutException:
            return False

    def wait_for_entity_flyout_content(self, timeout: int = 30) -> bool:
        self.install_entity_flyout_helpers()
        deadline = time.time() + timeout
        last_signature = ""
        stable_checks = 0
        while time.time() < deadline:
            state = self.require_driver().execute_script(
                """
                const root = window.__brandsightFindEntityFlyout?.();
                if (!root) return {ready: false, signature: '', textLength: 0, imageCount: 0, linkCount: 0, textPreview: ''};
                const text = (root.innerText || '').trim();
                const normalized = text.toLowerCase();
                const imageCount = root.querySelectorAll('img[src]').length;
                const linkCount = root.querySelectorAll('a[href]').length;
                const loading = normalized.includes('looking up') || normalized.includes('loading');
                const ready = !loading && text.length >= 30;
                return {
                  ready,
                  signature: `${text.length}:${imageCount}:${linkCount}:${text.slice(0, 120)}`,
                  textLength: text.length,
                  imageCount,
                  linkCount,
                  textPreview: text.slice(0, 160)
                };
                """
            )
            if not isinstance(state, dict):
                state = {}
            signature = str(state.get("signature") or "")
            if state.get("ready") and signature and signature == last_signature:
                stable_checks += 1
            elif state.get("ready") and signature:
                stable_checks = 0
                last_signature = signature
            else:
                stable_checks = 0
                last_signature = signature
            if stable_checks >= 2:
                return True
            time.sleep(0.5)
        return False

    def extract_entity_flyout(self, index: int) -> dict[str, Any]:
        self.install_entity_flyout_helpers()
        raw = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindEntityFlyout?.();
            const candidate = window.__brandsightLastEntityCandidate || {};
            if (!root) return null;
            const section = root.querySelector('[data-testid="screen-threadFlyOut"], section') || root;
            const title = (
              section.querySelector('h1,h2,h3,[data-testid*="title"]')?.innerText ||
              section.querySelector('[data-testid*="header"]')?.innerText ||
              candidate.text ||
              ''
            ).trim();
            const links = [...section.querySelectorAll('a[href]')].map((link, linkIndex) => ({
              index: linkIndex + 1,
              url: link.href,
              text: (link.innerText || '').trim()
            }));
            const images = [...section.querySelectorAll('img[src]')].map((image, imageIndex) => ({
              index: imageIndex + 1,
              src: image.src,
              alt: image.alt || ''
            }));
            return {
              raw_html: root.outerHTML || '',
              title,
              entity_text: candidate.text || '',
              text_length: (section.innerText || '').trim().length,
              link_count: links.length,
              image_count: images.length,
              links,
              images
            };
            """
        )
        if not isinstance(raw, dict):
            return {
                "index": index,
                "entity_index": index,
                "capture_method": "entity_flyout_outer_html",
                "raw_html": "",
                "html_length": 0,
                "title": "",
                "entity_text": "",
                "text_length": 0,
                "link_count": 0,
                "image_count": 0,
                "links": [],
                "images": [],
            }

        html = str(raw.get("raw_html") or "")
        return {
            "index": index,
            "entity_index": index,
            "capture_method": "entity_flyout_outer_html",
            "raw_html": html,
            "html_length": len(html),
            "title": clean_text(raw.get("title")),
            "entity_text": clean_text(raw.get("entity_text")),
            "text_length": int(raw.get("text_length") or 0),
            "link_count": int(raw.get("link_count") or 0),
            "image_count": int(raw.get("image_count") or 0),
            "links": raw.get("links") if isinstance(raw.get("links"), list) else [],
            "images": raw.get("images") if isinstance(raw.get("images"), list) else [],
        }

    def close_entity_flyout(self) -> None:
        driver = self.require_driver()
        self.install_entity_flyout_helpers()
        try:
            closed = driver.execute_script(
                """
                const root = window.__brandsightFindEntityFlyout?.();
                const closeButton = root?.querySelector('button[aria-label="Close"]');
                if (!closeButton) return false;
                closeButton.click();
                return true;
                """
            )
            if closed:
                time.sleep(0.4)
                return
        except WebDriverException:
            pass

        try:
            if driver.execute_script("return Boolean(window.__brandsightFindEntityFlyout?.());"):
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                time.sleep(0.4)
        except WebDriverException:
            pass

    def install_entity_flyout_helpers(self) -> None:
        self.require_driver().execute_script(
            """
            window.__brandsightCollectEntityOpeners = function (root) {
              const scope = root || document;
              const entities = [...scope.querySelectorAll('.entity-underline')]
                .filter((entity) => entity.isConnected && entity.getClientRects().length > 0);
              return entities.map((entity, index) => {
                const target = entity.closest('button,[role="button"],span,div') || entity;
                const text = (entity.innerText || entity.textContent || target.innerText || '').trim();
                return {index, entity, target, text, targetTag: target.tagName, targetTextPreview: (target.innerText || '').trim().slice(0, 120)};
              });
            };

            window.__brandsightDispatchEntityClick = function (target) {
              target.scrollIntoView({block: 'center', inline: 'center'});
              const rect = target.getBoundingClientRect();
              const options = {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: rect.left + Math.max(1, rect.width / 2),
                clientY: rect.top + Math.max(1, rect.height / 2),
                pointerType: 'mouse'
              };
              target.dispatchEvent(new PointerEvent('pointerover', options));
              target.dispatchEvent(new MouseEvent('mouseover', options));
              target.dispatchEvent(new PointerEvent('pointerdown', options));
              target.dispatchEvent(new MouseEvent('mousedown', options));
              target.dispatchEvent(new PointerEvent('pointerup', options));
              target.dispatchEvent(new MouseEvent('mouseup', options));
              target.dispatchEvent(new MouseEvent('click', options));
              return true;
            };

            window.__brandsightClickEntityOpener = function (root, index) {
              const candidate = window.__brandsightCollectEntityOpeners(root)[index];
              if (!candidate?.target) return false;
              window.__brandsightLastEntityCandidate = {index: candidate.index, text: candidate.text};
              return window.__brandsightDispatchEntityClick(candidate.target);
            };

            window.__brandsightFindEntityFlyout = function () {
              const flyouts = [
                ...document.querySelectorAll('[data-testid="stage-thread-flyout"], [data-testid="screen-threadFlyOut"]')
              ];
              return flyouts.find((flyout) => {
                if (flyout.querySelector('[data-testid="bar-product-header"], section[aria-label="Product details"], section[aria-label="Sources"]')) {
                  return false;
                }
                const text = (flyout.innerText || '').trim();
                return text.length > 0 && flyout.querySelector('button[aria-label="Close"]');
              }) || null;
            };
            """
        )

    def entity_button_diagnostics(self, scope: WebElement) -> str:
        self.install_entity_flyout_helpers()
        try:
            result = self.require_driver().execute_script(
                """
                const candidates = window.__brandsightCollectEntityOpeners?.(arguments[0]) || [];
                return {
                  candidateCount: candidates.length,
                  candidates: candidates.slice(0, 20).map((candidate) => ({
                    index: candidate.index + 1,
                    text: candidate.text,
                    targetTag: candidate.targetTag,
                    targetTextPreview: candidate.targetTextPreview
                  }))
                };
                """,
                scope,
            )
            return str(result)
        except WebDriverException:
            return "No entity diagnostics available."

    def entity_flyout_diagnostics(self) -> str:
        self.install_entity_flyout_helpers()
        result = self.require_driver().execute_script(
            """
            const root = window.__brandsightFindEntityFlyout?.() || null;
            return {
              found: Boolean(root),
              stageThreadFlyoutCount: document.querySelectorAll('[data-testid="stage-thread-flyout"]').length,
              screenThreadFlyoutCount: document.querySelectorAll('[data-testid="screen-threadFlyOut"]').length,
              entityUnderlineCount: document.querySelectorAll('.entity-underline').length,
              textPreview: root ? (root.innerText || '').slice(0, 500) : ''
            };
            """
        )
        return str(result)

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
        return self.require_driver().find_elements(By.CSS_SELECTOR, ASSISTANT_RESPONSE_SELECTOR)

    def response_action_container(self, response: WebElement) -> WebElement | None:
        for xpath in ["../..", "../../..", "../../../.."]:
            try:
                return response.find_element(By.XPATH, xpath)
            except NoSuchElementException:
                continue
        return None

    def latest_response_turn_element(self, response: WebElement) -> WebElement | None:
        try:
            return response.find_element(By.XPATH, "ancestor::section[@data-turn='assistant'][1]")
        except NoSuchElementException:
            return None

    def find_copy_button(self, container: WebElement) -> WebElement | None:
        buttons = container.find_elements(By.CSS_SELECTOR, "button")
        for button in reversed(buttons):
            label = " ".join(
                filter(
                    None,
                    [
                        button.get_attribute("aria-label"),
                        button.get_attribute("data-testid"),
                        button.get_attribute("title"),
                    ],
                )
            ).lower()
            if "sources" in label or "select product" in label:
                continue
            html = button.get_attribute("outerHTML") or ""
            if "copy" in label or "clip-rule" in html and "M7 5a3 3" in html:
                if button.is_displayed() and button.is_enabled():
                    return button
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
            raise RuntimeError("Browser has not been started")
        return self.driver


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


def normalize_words(value: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.'-]*", value or "") if len(word) > 2]


def clean_chatgpt_source_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not (key == "utm_source" and value == "chatgpt.com")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def extract_sources_from_markdown(markdown: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    reference_pattern = re.compile(
        r"""^\s*\[(?P<ref>[^\]]+)\]:\s+(?P<url>\S+)(?:\s+(?P<title>"[^"]*"|'[^']*'|\([^)]+\)))?\s*$""",
        re.MULTILINE,
    )
    for match in reference_pattern.finditer(markdown or ""):
        raw_url = match.group("url").strip()
        if not is_external_url(raw_url) or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        title = clean_markdown_link_title(match.group("title"))
        sources.append(
            build_markdown_source(
                index=len(sources) + 1,
                raw_url=raw_url,
                title=title,
                reference_id=match.group("ref").strip(),
            )
        )

    inline_pattern = re.compile(r"""(?<!!)\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)(?:\s+(?P<title>"[^"]*"|'[^']*'))?\)""")
    for match in inline_pattern.finditer(markdown or ""):
        raw_url = match.group("url").strip()
        if not is_external_url(raw_url) or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        title = clean_markdown_link_title(match.group("title")) or clean_text(match.group("label"))
        sources.append(
            build_markdown_source(
                index=len(sources) + 1,
                raw_url=raw_url,
                title=title,
                reference_id=None,
            )
        )

    return sources


def build_markdown_source(index: int, raw_url: str, title: str, reference_id: str | None) -> dict[str, Any]:
    clean_url = clean_chatgpt_source_url(raw_url)
    domain = urlsplit(clean_url).netloc.replace("www.", "")
    return {
        "index": index,
        "url": raw_url,
        "clean_url": clean_url,
        "source": domain,
        "title": title,
        "description": "",
        "favicon_url": f"https://www.google.com/s2/favicons?domain={domain}&sz=32" if domain else None,
        "reference_id": reference_id,
        "extraction_source": "markdown_reference",
    }


def clean_markdown_link_title(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    elif value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    return clean_text(value)


def is_external_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")
