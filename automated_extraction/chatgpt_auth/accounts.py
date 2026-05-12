"""Account credential decoding for automated ChatGPT login.

Mirrors the AccountsDeserializer pattern from
`daily-coding-problem/chatgpt-scraper-lib`, but reads the base64 JSON blob
from BrandSight's `CHATGPT_ACCOUNTS_B64` env var instead of `TEST_ACCOUNTS`.

Expected decoded shape:

    {
        "user@example.com": {
            "provider": "basic" | "google",
            "password": "...",
            "secret": {
                "chatgpt": "TOTP_BASE32_SECRET",
                "google":  "TOTP_BASE32_SECRET"
            }
        }
    }
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)

ENV_VAR = "CHATGPT_ACCOUNTS_B64"


class AccountsDeserializer:
    def __init__(self, base64_string: str | None = None) -> None:
        if base64_string is None:
            base64_string = os.environ.get(ENV_VAR, "")
        self._accounts: dict[str, dict[str, Any]] = self._deserialize(base64_string)

    @staticmethod
    def _deserialize(base64_string: str | None) -> dict[str, dict[str, Any]]:
        if not base64_string:
            return {}

        try:
            decoded_bytes = base64.b64decode(base64_string, validate=True)
            decoded_string = decoded_bytes.decode("utf-8")
            payload = json.loads(decoded_string)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid {ENV_VAR}. Expected base64-encoded JSON: {error}") from error

        if not isinstance(payload, dict):
            raise ValueError(f"Invalid {ENV_VAR}. Top-level JSON must be an object keyed by email.")

        accounts: dict[str, dict[str, Any]] = {}
        for email, account in payload.items():
            if not isinstance(account, dict):
                LOGGER.warning("Ignoring non-object account entry for %s", email)
                continue
            accounts[str(email)] = account
        return accounts

    def get_account(self, email: str) -> dict[str, Any] | None:
        return self._accounts.get(email)

    def get_all_accounts(self) -> dict[str, dict[str, Any]]:
        return dict(self._accounts)

    def __contains__(self, email: object) -> bool:
        return isinstance(email, str) and email in self._accounts

    def __bool__(self) -> bool:
        return bool(self._accounts)

    def __len__(self) -> int:
        return len(self._accounts)

    def __str__(self) -> str:
        # Never log raw passwords / secrets; emit just the email keys.
        return f"AccountsDeserializer(emails={sorted(self._accounts.keys())})"
