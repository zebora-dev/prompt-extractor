"""Basic (email + password) ChatGPT login.

Ported from `daily-coding-problem/chatgpt-scraper-lib`'s
`chatgpt/auth/methods/basic_login.py`. Targets Auth0's `auth.openai.com`
hosted login. The selectors below are the most likely maintenance point
when the login UI changes -- update them in one place.
"""

from __future__ import annotations

import logging
from typing import Any

from .login_method import LoginMethod
from .otp import Providers, generate_otp


LOGGER = logging.getLogger(__name__)

EMAIL_INPUT_SELECTOR = "input[id='email-input'], input[name='email'], input[type='email']"
CONTINUE_BUTTON_SELECTOR = (
    "button[class*='continue-btn'], button[type='submit'][name='intent'][value='email'], button[type='submit']"
)
PASSWORD_INPUT_SELECTOR = "input[id='password'], input[name='password'], input[type='password']"
SUBMIT_BUTTON_SELECTOR = "button[type='submit']"
CODE_TOKEN_INPUT_SELECTOR = "input[id='code'], input[name='code'], input[autocomplete='one-time-code']"


class BasicLogin(LoginMethod):
    def login(self, email: str, account: dict[str, Any]) -> bool:
        if not self.extract_account_info(email, account):
            return False

        LOGGER.info("BasicLogin: entering email for %s. %s", email, self.page_diagnostics())
        if not self.enter_email(self.email or "", EMAIL_INPUT_SELECTOR, CONTINUE_BUTTON_SELECTOR):
            LOGGER.error("BasicLogin: failed to submit email. %s", self.page_diagnostics())
            return False

        LOGGER.info("BasicLogin: entering password. %s", self.page_diagnostics())
        if not self.enter_password(self.password or "", PASSWORD_INPUT_SELECTOR, SUBMIT_BUTTON_SELECTOR):
            LOGGER.error("BasicLogin: failed to submit password. %s", self.page_diagnostics())
            return False

        chatgpt_otp = self.otp_auth.get(Providers.CHATGPT.value)
        if chatgpt_otp:
            LOGGER.info("BasicLogin: entering 2FA token")
            otp_token = generate_otp(chatgpt_otp.get_secret())
            if not self.enter_2fa_token(otp_token, CODE_TOKEN_INPUT_SELECTOR, SUBMIT_BUTTON_SELECTOR):
                LOGGER.error("BasicLogin: failed to submit 2FA token")
                return False

        return True
