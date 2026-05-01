from __future__ import annotations

import logging
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
from selenium.common.exceptions import ElementClickInterceptedException, JavascriptException, NoSuchElementException, SessionNotCreatedException, TimeoutException, WebDriverException
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
    capture_method: str
    raw_html: str
    raw_html_capture_method: str
    llm_model: str
    url: str
    sources: list[dict[str, Any]]
    source_capture_method: str


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
    ) -> None:
        self.chatgpt_url = chatgpt_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.login_wait_seconds = login_wait_seconds
        self.response_timeout_seconds = response_timeout_seconds
        self.sources_panel_pause_seconds = max(0, sources_panel_pause_seconds)
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
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.driver.get(self.chatgpt_url)
        self.wait_for_login()

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
        response = self.capture_latest_response()
        if not response or response.strip() == prompt_text.strip():
            time.sleep(2)
            response = self.capture_latest_response()
        if not response or len(response.strip()) < 20:
            raise RuntimeError("Captured response was empty or too short")
        LOGGER.info("Markdown copied from ChatGPT response. length=%s method=%s", len(response.strip()), "copy_button_markdown")
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
            capture_method="copy_button_markdown",
            raw_html=raw_html,
            raw_html_capture_method=raw_html_capture_method,
            llm_model=llm_model,
            url=driver.current_url,
            sources=sources,
            source_capture_method=source_capture_method,
        )

    def wait_for_login(self) -> None:
        deadline = time.time() + self.login_wait_seconds
        while time.time() < deadline:
            if self.find_first(CHAT_INPUT_SELECTORS):
                return
            time.sleep(1)
        raise TimeoutError(
            "Timed out waiting for ChatGPT prompt input. Log in in the opened browser or set CHATGPT_CHROME_USER_DATA_DIR to a logged-in profile."
        )

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
        time.sleep(random.uniform(0.2, 0.5))

        for char in prompt_text:
            if char == "\n":
                input_element.send_keys(Keys.SHIFT, Keys.ENTER)
            else:
                input_element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.2))

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
        try:
            WebDriverWait(self.require_driver(), timeout).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, STOP_BUTTON_SELECTOR))
            )
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

    def capture_latest_response(self) -> str:
        latest_response = self.latest_response_element()
        if not latest_response:
            return ""

        parent = self.response_action_container(latest_response)
        if parent:
            copy_button = self.find_copy_button(parent)
            if copy_button:
                marker = f"__brandsight_capture_marker_{time.time_ns()}__"
                pyperclip.copy(marker)
                self.click_element(copy_button)
                deadline = time.time() + 3
                while time.time() < deadline:
                    copied = pyperclip.paste()
                    if copied and copied != marker:
                        return copied
                    time.sleep(0.2)

        return latest_response.text or ""

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

    def latest_response_text(self) -> str:
        element = self.latest_response_element()
        return element.text.strip() if element else ""

    def latest_response_element(self) -> WebElement | None:
        responses = self.response_elements()
        return responses[-1] if responses else None

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
