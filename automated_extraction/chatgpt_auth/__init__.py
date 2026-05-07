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


def __getattr__(name: str):
    if name == "BasicLogin":
        from .basic_login import BasicLogin

        return BasicLogin
    if name == "GoogleLogin":
        from .google_login import GoogleLogin

        return GoogleLogin
    if name == "LoginMethod":
        from .login_method import LoginMethod

        return LoginMethod
    if name in {"OTPAuth", "Providers", "generate_otp"}:
        from .otp import OTPAuth, Providers, generate_otp

        return {
            "OTPAuth": OTPAuth,
            "Providers": Providers,
            "generate_otp": generate_otp,
        }[name]
    if name in {"AutomatedLoginError", "perform_automated_login"}:
        from .runner import AutomatedLoginError, perform_automated_login

        return {
            "AutomatedLoginError": AutomatedLoginError,
            "perform_automated_login": perform_automated_login,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
