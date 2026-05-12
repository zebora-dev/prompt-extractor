"""TOTP / OTPAuth helpers.

Direct port of the `OTPAuth` parser and `generate_otp` helper from
`daily-coding-problem/chatgpt-scraper-lib`. Used by BasicLogin / GoogleLogin
to emit time-based 2FA codes when an account has a `secret` configured.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pyotp


class Providers(StrEnum):
    GOOGLE = "google"
    CHATGPT = "chatgpt"


def generate_otp(secret_key: str) -> str:
    """Generate a current TOTP code for the given base32 secret."""
    return pyotp.TOTP(secret_key).now()


class OTPAuth:
    """Parse an `otpauth://totp/Issuer:user?secret=...&issuer=...` URI."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.environment: str | None = None
        self.user: str | None = None
        self.talent_id: str | None = None
        self.secret: str | None = None
        self.issuer: str | None = None
        self.algorithm: str = "SHA1"
        self.digits: int = 6
        self.period: int = 30
        self._parse_uri()

    def _parse_uri(self) -> None:
        result = urlparse(self.uri)

        path_parts = result.path.strip("/").split(":")
        if len(path_parts) != 2 or not path_parts[1]:
            raise ValueError("Invalid OTPAuth URI: path must be 'environment:user'")

        self.environment = path_parts[0]
        if "@" in path_parts[1]:
            self.talent_id = path_parts[1].split("@")[0]
        else:
            self.user = unquote(path_parts[1])

        parameters = parse_qs(result.query)
        if "secret" not in parameters or "issuer" not in parameters:
            raise ValueError("OTPAuth URI is missing required 'secret' or 'issuer'")

        self.secret = parameters["secret"][0]
        self.issuer = parameters["issuer"][0]

        if "algorithm" in parameters:
            self.algorithm = parameters["algorithm"][0]
        if "digits" in parameters:
            self.digits = int(parameters["digits"][0])
        if "period" in parameters:
            self.period = int(parameters["period"][0])

    def get_secret(self) -> str:
        if not self.secret:
            raise ValueError("OTPAuth secret is not set")
        return self.secret

    def get_issuer(self) -> str | None:
        return self.issuer

    @staticmethod
    def construct_otp_uri(
        email: str,
        secret: str,
        issuer: str = "OpenAI",
        algorithm: str = "SHA1",
        digits: int = 6,
        period: int = 30,
    ) -> str:
        label = f"{issuer}:{email}"
        params = {
            "secret": secret,
            "issuer": issuer,
            "algorithm": algorithm,
            "digits": digits,
            "period": period,
        }
        return f"otpauth://totp/{label}?{urlencode(params)}"

    @staticmethod
    def derive_issuer_by_provider(provider: str) -> str:
        if provider.lower() == Providers.GOOGLE.value:
            return "Google"
        return "OpenAI"
