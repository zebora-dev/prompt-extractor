"""Login method abstraction for automated ChatGPT login.

Ported from `daily-coding-problem/chatgpt-scraper-lib`'s
`chatgpt/auth/login_method.py` and `chatgpt/element_interactor.py`. The
reference repo wraps Selenium in its own `Browser` + `ElementInteractor`;
here we operate directly on the existing `selenium.webdriver.Chrome`
instance owned by `ChatGPTRunner` so we don't duplicate driver lifecycles.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .otp import OTPAuth, Providers


LOGGER = logging.getLogger(__name__)

LOGIN_BUTTON_SELECTOR = "button[data-testid='login-button']"
DEFAULT_ELEMENT_TIMEOUT = 20


class ElementInteractor:
    """Thin Selenium helper used by login flows.

    Provides the same surface (`find_element`, `wait_for_element`,
    `interact_with_element`, `click_element`) as the reference repo's
    `chatgpt/element_interactor.py` so that `LoginMethod` subclasses can be
    ported with minimal change. Implements the JS click fallback and human-
    paced typing already used by `ChatGPTRunner`.
    """

    def __init__(self, driver: WebDriver, *, default_timeout: int = DEFAULT_ELEMENT_TIMEOUT) -> None:
        self.driver = driver
        self.default_timeout = default_timeout

    def wait_for_element(self, by: str, selector: str, timeout: int | None = None) -> WebElement | None:
        try:
            return WebDriverWait(self.driver, timeout or self.default_timeout).until(
                EC.presence_of_element_located((by, selector))
            )
        except TimeoutException:
            return None

    def wait_for_clickable(self, by: str, selector: str, timeout: int | None = None) -> WebElement | None:
        try:
            return WebDriverWait(self.driver, timeout or self.default_timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
        except TimeoutException:
            return None

    def find_element(self, by: str, selector: str) -> WebElement | None:
        try:
            return self.driver.find_element(by, selector)
        except WebDriverException:
            return None

    def find_elements(self, by: str, selector: str) -> list[WebElement]:
        try:
            return list(self.driver.find_elements(by, selector))
        except WebDriverException:
            return []

    def visible_text_preview(self, limit: int = 800) -> str:
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            return ""
        return " ".join((text or "").split())[:limit]

    def type_text(self, element: WebElement, text: str) -> bool:
        # Scroll into view before interacting — important on VNC where the element
        # may be off-screen and click/focus silently no-ops.
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.2)
        except WebDriverException:
            pass

        try:
            element.click()
            time.sleep(0.3)
        except (ElementClickInterceptedException, WebDriverException):
            try:
                self.driver.execute_script("arguments[0].focus();", element)
                time.sleep(0.3)
            except WebDriverException:
                return False

        # Clear with keyboard (Ctrl+A → Delete) so Angular/React change detection
        # fires correctly. element.clear() bypasses the browser's input event pipeline
        # and leaves Google's form in a broken state where Next stays disabled.
        try:
            element.send_keys(Keys.CONTROL + "a")
            element.send_keys(Keys.DELETE)
        except WebDriverException:
            try:
                element.clear()
            except (StaleElementReferenceException, WebDriverException):
                pass

        # Prefer execCommand('insertText') — fires a native InputEvent that Angular
        # picks up, and avoids one send_keys round-trip per character over VNC.
        try:
            inserted = self.driver.execute_script(
                "arguments[0].focus(); return document.execCommand('insertText', false, arguments[1]);",
                element,
                text,
            )
            if inserted:
                return True
        except WebDriverException:
            pass

        # Fallback: character-by-character.
        for char in text:
            try:
                element.send_keys(char)
            except WebDriverException as error:
                LOGGER.debug("send_keys failed for char %r: %s", char, error)
                return False
            time.sleep(random.uniform(0.04, 0.16))
        return True

    def click_element(self, element: WebElement) -> bool:
        try:
            ActionChains(self.driver).move_to_element(element).pause(0.1).click().perform()
            return True
        except (
            ElementClickInterceptedException,
            JavascriptException,
            StaleElementReferenceException,
            WebDriverException,
        ):
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except WebDriverException as error:
                LOGGER.debug("JS click fallback failed: %s", error)
                return False

    def interact_with_element(
        self,
        by: str,
        selector: str,
        *,
        text: str | None = None,
        timeout: int | None = None,
    ) -> bool:
        element = self.wait_for_clickable(by, selector, timeout=timeout) or self.wait_for_element(by, selector, timeout=timeout)
        if not element:
            LOGGER.error("Element not found: by=%s selector=%s", by, selector)
            return False
        if text is not None:
            return self.type_text(element, text)
        return self.click_element(element)


class LoginMethod(ABC):
    """Abstract login method. Concrete subclasses implement `login`."""

    def __init__(self, driver: WebDriver, otp_uri: str | None = None) -> None:
        self.driver = driver
        self.element_interactor = ElementInteractor(driver)
        self.email: str | None = None
        self.password: str | None = None
        self.otp_auth: dict[str, OTPAuth] = {}

        if otp_uri:
            parsed = OTPAuth(otp_uri)
            issuer = (parsed.issuer or "").lower()
            if issuer:
                self.otp_auth[issuer] = parsed

    @abstractmethod
    def login(self, email: str, account: dict[str, Any]) -> bool:
        raise NotImplementedError

    @staticmethod
    def derive_login_provider(account: dict[str, Any]) -> type["LoginMethod"]:
        # Late imports avoid the circular import via `chatgpt_auth.__init__`.
        from .basic_login import BasicLogin
        from .google_login import GoogleLogin

        provider = (account.get("provider") or "basic").lower()
        if provider == Providers.GOOGLE.value:
            return GoogleLogin
        return BasicLogin

    def extract_account_info(self, email: str, account: dict[str, Any]) -> bool:
        if not email:
            LOGGER.error("Login email not provided")
            return False
        if not account:
            LOGGER.error("Account dictionary is empty for %s", email)
            return False

        self.email = email
        self.password = account.get("password")
        if not self.password:
            LOGGER.error("Password not found for account %s", email)
            return False

        secrets = account.get("secret") or {}
        if isinstance(secrets, dict):
            self.otp_auth = {}
            for provider, secret in secrets.items():
                if not secret:
                    LOGGER.warning("No TOTP secret for provider %s on account %s", provider, email)
                    continue
                issuer = OTPAuth.derive_issuer_by_provider(str(provider))
                otp_uri = OTPAuth.construct_otp_uri(email, str(secret), issuer=issuer)
                self.otp_auth[str(provider).lower()] = OTPAuth(otp_uri)
        return True

    def find_element(self, by_type: str, selector: str) -> WebElement | None:
        return self.element_interactor.find_element(by_type or By.CSS_SELECTOR, selector)

    def wait_for_element(self, by_type: str, selector: str, timeout: int | None = None) -> WebElement | None:
        return self.element_interactor.wait_for_element(by_type or By.CSS_SELECTOR, selector, timeout=timeout)

    def wait_for_clickable(self, by_type: str, selector: str, timeout: int | None = None) -> WebElement | None:
        return self.element_interactor.wait_for_clickable(by_type or By.CSS_SELECTOR, selector, timeout=timeout)

    def click_element(self, by_type: str, selector: str) -> bool:
        return self.element_interactor.interact_with_element(by_type or By.CSS_SELECTOR, selector)

    def click_optional(self, by_type: str, selector: str, timeout: int = 5) -> bool:
        element = self.wait_for_clickable(by_type, selector, timeout=timeout) or self.wait_for_element(
            by_type, selector, timeout=timeout
        )
        return self.element_interactor.click_element(element) if element else False

    def page_diagnostics(self) -> str:
        try:
            url = self.driver.current_url
        except WebDriverException:
            url = "<unknown>"
        try:
            title = self.driver.title
        except WebDriverException:
            title = "<unknown>"
        preview = self.element_interactor.visible_text_preview()
        return f"url={url!r} title={title!r} text_preview={preview!r}"

    def _enter_and_click(
        self,
        text: str,
        input_selector: str,
        button_selector: str,
        use_xpath: bool,
    ) -> bool:
        by_type = By.XPATH if use_xpath else By.CSS_SELECTOR
        return self.element_interactor.interact_with_element(
            by_type, input_selector, text=text
        ) and self.element_interactor.interact_with_element(by_type, button_selector)

    def enter_email(self, email: str, input_selector: str, button_selector: str, use_xpath: bool = False) -> bool:
        return self._enter_and_click(email, input_selector, button_selector, use_xpath)

    def enter_password(self, password: str, input_selector: str, button_selector: str, use_xpath: bool = False) -> bool:
        return self._enter_and_click(password, input_selector, button_selector, use_xpath)

    def enter_2fa_token(self, token: str, code_input_selector: str, button_selector: str, use_xpath: bool = False) -> bool:
        return self._enter_and_click(token, code_input_selector, button_selector, use_xpath)
