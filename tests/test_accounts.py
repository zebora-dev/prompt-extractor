"""Tests for CHATGPT_ACCOUNTS_B64 credential decoding."""

from __future__ import annotations

import base64
import json

import pytest

from automated_extraction.chatgpt_auth.accounts import AccountsDeserializer


def _encode(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


VALID_ACCOUNTS = {
    "grant@theround.com": {"provider": "google", "password": "pass1", "secret": {"google": "TOTP1"}},
    "dev@zebora.io": {"provider": "basic", "password": "pass2", "secret": {"chatgpt": "TOTP2"}},
}


class TestAccountsDeserializer:
    def test_valid_accounts_decoded(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        assert len(d) == 2
        assert "grant@theround.com" in d
        assert "dev@zebora.io" in d

    def test_get_account_returns_correct_record(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        account = d.get_account("dev@zebora.io")
        assert account is not None
        assert account["provider"] == "basic"
        assert account["password"] == "pass2"

    def test_get_account_missing_returns_none(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        assert d.get_account("unknown@example.com") is None

    def test_contains_operator(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        assert "grant@theround.com" in d
        assert "nobody@example.com" not in d

    def test_bool_true_when_populated(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        assert bool(d) is True

    def test_bool_false_when_empty(self):
        d = AccountsDeserializer(_encode({}))
        assert bool(d) is False

    def test_empty_string_returns_empty(self):
        d = AccountsDeserializer("")
        assert len(d) == 0

    def test_invalid_base64_raises(self):
        with pytest.raises(ValueError, match="Invalid CHATGPT_ACCOUNTS_B64"):
            AccountsDeserializer("not-valid-base64!!!")

    def test_invalid_json_raises(self):
        bad = base64.b64encode(b"not json").decode()
        with pytest.raises(ValueError, match="Invalid CHATGPT_ACCOUNTS_B64"):
            AccountsDeserializer(bad)

    def test_non_dict_json_raises(self):
        bad = base64.b64encode(json.dumps(["list"]).encode()).decode()
        with pytest.raises(ValueError, match="Invalid CHATGPT_ACCOUNTS_B64"):
            AccountsDeserializer(bad)

    def test_str_repr_hides_passwords(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        result = str(d)
        assert "pass1" not in result
        assert "pass2" not in result
        assert "grant@theround.com" in result

    def test_get_all_accounts(self):
        d = AccountsDeserializer(_encode(VALID_ACCOUNTS))
        all_accounts = d.get_all_accounts()
        assert set(all_accounts.keys()) == {"grant@theround.com", "dev@zebora.io"}
