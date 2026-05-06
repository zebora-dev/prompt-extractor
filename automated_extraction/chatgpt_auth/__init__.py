"""Automated ChatGPT login package.

Public surface:

- `AccountsDeserializer`: decode CHATGPT_ACCOUNTS_B64 into account dicts.
- `LoginMethod`: abstract base class shared by Basic/Google flows.
- `BasicLogin`, `GoogleLogin`: concrete login methods.
- `OTPAuth`, `generate_otp`, `Providers`: 2FA helpers.
- `perform_automated_login`: orchestrator called from `ChatGPTRunner`.
- `AutomatedLoginError`: raised on misconfiguration / login failure.
"""

from __future__ import annotations

from .accounts import AccountsDeserializer
from .basic_login import BasicLogin
from .google_login import GoogleLogin
from .login_method import LoginMethod
from .otp import OTPAuth, Providers, generate_otp
from .runner import AutomatedLoginError, perform_automated_login


__all__ = [
    "AccountsDeserializer",
    "AutomatedLoginError",
    "BasicLogin",
    "GoogleLogin",
    "LoginMethod",
    "OTPAuth",
    "Providers",
    "generate_otp",
    "perform_automated_login",
]
