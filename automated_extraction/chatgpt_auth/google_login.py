"""Google SSO ChatGPT login.

Ported from `daily-coding-problem/chatgpt-scraper-lib`'s
`chatgpt/auth/methods/google_login.py`. Walks the "Continue with Google"
flow on chatgpt.com -> accounts.google.com, including the "Try another way"
-> "Authenticator" branch and the optional secondary ChatGPT 2FA screen.
Selectors live as XPath constants at the top of this file because the
Google sign-in DOM is structured around translatable button text.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from selenium.webdriver.common.by import By

from .login_method import LoginMethod
from .otp import Providers, generate_otp

LOGGER = logging.getLogger(__name__)

GOOGLE_LOGIN_BUTTON_XPATH = (
    "//*[self::button or self::a or @role='button']"
    "[contains(normalize-space(.), 'Continue with Google')"
    " or contains(normalize-space(.), 'Sign in with Google')"
    " or contains(normalize-space(.), 'Google')]"
)
EMAIL_INPUT_XPATH = "//input[@type='email']"
PASSWORD_INPUT_XPATH = "//input[@type='password']"
NEXT_BUTTON_XPATH = (
    "//*[self::button or @role='button']"
    "[.//span[contains(normalize-space(.), 'Next')] or contains(normalize-space(.), 'Next')]"
)
USE_ANOTHER_ACCOUNT_XPATH = (
    "//*[self::div or self::li or self::button or @role='button'][contains(normalize-space(.), 'Use another account')]"
)
TRY_ANOTHER_WAY_LINK_XPATH = "//*[self::button or @role='button'][contains(normalize-space(.), 'Try another way')]"
SELECT_AUTHENTICATOR_APP_XPATH = (
    "//*[self::li or self::div or @role='link' or @role='button']"
    "[contains(normalize-space(.), 'Google Authenticator')"
    " or contains(normalize-space(.), 'Authenticator')]"
)
CODE_TOKEN_INPUT_XPATH = "//input[@id='totpPin' or @name='totpPin' or @type='tel' or @autocomplete='one-time-code']"

# ChatGPT may impose a secondary 2FA prompt of its own after Google SSO
# completes; this is independent of the Google Authenticator step above.
VERIFY_YOUR_IDENTITY_XPATH = "//*[contains(text(), 'Verify Your Identity')]"
CHATGPT_CODE_TOKEN_INPUT_XPATH = "//input[@name='code']"
SUBMIT_BUTTON_XPATH = "//button[@type='submit']"


class GoogleLogin(LoginMethod):
    def login(self, email: str, account: dict[str, Any]) -> bool:
        if not self.extract_account_info(email, account):
            return False

        LOGGER.info("GoogleLogin: clicking 'Continue with Google'")
        if not self.click_optional(By.XPATH, GOOGLE_LOGIN_BUTTON_XPATH, timeout=15):
            LOGGER.error("GoogleLogin: 'Continue with Google' button not found. %s", self.page_diagnostics())
            return False

        if not self._select_or_enter_email(email):
            return False

        LOGGER.info("GoogleLogin: entering Google password")
        if not self._enter_password_if_present():
            return False

        return self._handle_2fa()

    def _select_or_enter_email(self, email: str) -> bool:
        if self.wait_for_element(By.XPATH, PASSWORD_INPUT_XPATH, timeout=5):
            LOGGER.info("GoogleLogin: password screen already visible; skipping email step")
            return True

        LOGGER.info("GoogleLogin: checking Google account chooser for %s", email)
        account_xpath = (
            f"//*[contains(@data-email, {xpath_literal(email)})"
            f" or contains(@aria-label, {xpath_literal(email)})"
            f" or contains(normalize-space(.), {xpath_literal(email)})]"
        )
        if self.click_optional(By.XPATH, account_xpath, timeout=5):
            LOGGER.info("GoogleLogin: selected existing Google account for %s", email)
            return True

        self.click_optional(By.XPATH, USE_ANOTHER_ACCOUNT_XPATH, timeout=3)
        if self.wait_for_element(By.XPATH, PASSWORD_INPUT_XPATH, timeout=2):
            LOGGER.info("GoogleLogin: password screen visible after account chooser")
            return True

        LOGGER.info("GoogleLogin: entering Google email for %s", email)
        if not self.enter_email(self.email or "", EMAIL_INPUT_XPATH, NEXT_BUTTON_XPATH, use_xpath=True):
            LOGGER.error("GoogleLogin: failed to submit Google email. %s", self.page_diagnostics())
            return False
        return True

    def _enter_password_if_present(self) -> bool:
        password_input = self.wait_for_element(By.XPATH, PASSWORD_INPUT_XPATH, timeout=20)
        if not password_input:
            LOGGER.info("GoogleLogin: password input not shown; assuming Google reused an existing session")
            return True
        if not self.enter_password(self.password or "", PASSWORD_INPUT_XPATH, NEXT_BUTTON_XPATH, use_xpath=True):
            LOGGER.error("GoogleLogin: failed to submit Google password. %s", self.page_diagnostics())
            return False
        return True

    def _handle_2fa(self) -> bool:
        time.sleep(2)

        if self.wait_for_element(By.XPATH, CODE_TOKEN_INPUT_XPATH, timeout=3):
            return self._submit_google_otp()

        try_another = self.click_optional(By.XPATH, TRY_ANOTHER_WAY_LINK_XPATH, timeout=5)
        if not try_another:
            LOGGER.info("GoogleLogin: 'Try another way' not present; skipping 2FA branch")
            return True

        if not self.click_optional(By.XPATH, SELECT_AUTHENTICATOR_APP_XPATH, timeout=10):
            LOGGER.warning("GoogleLogin: 'Google Authenticator' option not selectable. %s", self.page_diagnostics())
            return False

        return self._submit_google_otp()

    def _submit_google_otp(self) -> bool:
        google_otp = self.otp_auth.get(Providers.GOOGLE.value)
        if not google_otp:
            LOGGER.error("GoogleLogin: Google authenticator prompt present but no google TOTP secret configured")
            return False

        LOGGER.info("GoogleLogin: submitting Google Authenticator code")
        otp_token = generate_otp(google_otp.get_secret())
        if not self.enter_2fa_token(otp_token, CODE_TOKEN_INPUT_XPATH, NEXT_BUTTON_XPATH, use_xpath=True):
            LOGGER.error("GoogleLogin: failed to submit Google Authenticator code. %s", self.page_diagnostics())
            return False

        time.sleep(2)
        if self.find_element(By.XPATH, VERIFY_YOUR_IDENTITY_XPATH):
            chatgpt_otp = self.otp_auth.get(Providers.CHATGPT.value)
            if not chatgpt_otp:
                LOGGER.error("GoogleLogin: ChatGPT secondary 2FA screen present but no chatgpt secret configured")
                return False
            LOGGER.info("GoogleLogin: submitting secondary ChatGPT 2FA code")
            chatgpt_token = generate_otp(chatgpt_otp.get_secret())
            if not self.enter_2fa_token(
                chatgpt_token,
                CHATGPT_CODE_TOKEN_INPUT_XPATH,
                SUBMIT_BUTTON_XPATH,
                use_xpath=True,
            ):
                LOGGER.error("GoogleLogin: failed to submit secondary ChatGPT 2FA code. %s", self.page_diagnostics())
                return False

        return True


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"
