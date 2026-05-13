"""Automated ChatGPT login orchestrator.

Bridges the `LoginMethod` ABC to `ChatGPTRunner` by:

1. Looking up the credential record for the configured email.
2. Picking the right `LoginMethod` subclass via `derive_login_provider`.
3. Clicking the chatgpt.com landing-page login button.
4. Running the login flow.
5. Confirming the prompt textarea appears (the runner does its own
   final wait-for-input afterwards too).

The reference repo's `ChatGPTInteraction.login(...)` is the shape we
mirror, but here we operate directly on the existing Selenium driver
owned by `ChatGPTRunner` so there's no second browser lifecycle.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .login_method import LOGIN_BUTTON_SELECTOR, ElementInteractor, LoginMethod

LOGGER = logging.getLogger(__name__)

CHAT_INPUT_READY_SELECTOR = "textarea[id='prompt-textarea'], div[id='prompt-textarea'][contenteditable='true']"


class AutomatedLoginError(RuntimeError):
    """Raised when the automated login flow cannot complete."""


def perform_automated_login(
    driver: WebDriver,
    *,
    accounts: Mapping[str, dict[str, Any]],
    email: str,
    login_wait_seconds: int = 180,
    pre_login_pause_seconds: int = 0,
) -> bool:
    """Run the automated login flow for `email`.

    Returns True on success. Raises `AutomatedLoginError` on misconfiguration
    or when the chosen login method reports failure.
    """
    import os

    machine_id = os.getenv("FLY_MACHINE_ID", "local")
    LOGGER.info(
        "AUTO-LOGIN starting on machine=%s for email=%s — "
        "connect VNC now at https://prompt-extractor-us.fly.dev/vnc.html "
        "(set cookie fly_instance_id=%s)",
        machine_id,
        email,
        machine_id,
    )
    if pre_login_pause_seconds > 0:
        LOGGER.info("Pausing %ds before login to allow VNC connection", pre_login_pause_seconds)
        time.sleep(pre_login_pause_seconds)

    if not accounts:
        raise AutomatedLoginError(
            "Automated login requested but no accounts are configured. "
            "Set CHATGPT_ACCOUNTS_B64 to a base64-encoded JSON map of accounts."
        )
    if not email:
        raise AutomatedLoginError("Automated login requested but CHATGPT_LOGIN_EMAIL is not set.")

    account = accounts.get(email)
    if not account:
        raise AutomatedLoginError(
            f"Account {email!r} not found in CHATGPT_ACCOUNTS_B64. Available emails: {sorted(accounts.keys())}"
        )

    if _chat_input_present(driver):
        LOGGER.info("ChatGPT prompt textarea already visible; skipping automated login")
        return True

    interactor = ElementInteractor(driver)
    if not _click_login_button(interactor):
        LOGGER.info("Login button not found; assuming login is already in progress")

    method_cls = LoginMethod.derive_login_provider(account)
    method = method_cls(driver)
    LOGGER.info("Running %s for %s", method_cls.__name__, email)

    try:
        success = method.login(email, account)
    except WebDriverException as error:
        raise AutomatedLoginError(f"Selenium error during {method_cls.__name__} for {email}: {error}") from error

    if not success:
        raise AutomatedLoginError(
            f"{method_cls.__name__} reported failure for {email}. "
            f"Selectors may have drifted; see automated_extraction/chatgpt_auth/."
        )

    if not _wait_for_chat_input(driver, login_wait_seconds):
        raise AutomatedLoginError(
            f"Login flow finished but ChatGPT prompt textarea did not appear within "
            f"{login_wait_seconds}s. The session may require manual verification."
        )
    LOGGER.info("Automated login for %s completed successfully", email)
    return True


def _click_login_button(interactor: ElementInteractor) -> bool:
    button = interactor.wait_for_clickable(By.CSS_SELECTOR, LOGIN_BUTTON_SELECTOR, timeout=10)
    if not button:
        return False
    return interactor.click_element(button)


def _chat_input_present(driver: WebDriver) -> bool:
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, CHAT_INPUT_READY_SELECTOR)
    except WebDriverException:
        return False
    for element in elements:
        try:
            if element.is_displayed():
                return True
        except WebDriverException:
            continue
    return False


def _wait_for_chat_input(driver: WebDriver, timeout: int) -> bool:
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, CHAT_INPUT_READY_SELECTOR)))
            if _chat_input_present(driver):
                return True
        except TimeoutException:
            time.sleep(1)
    return False
