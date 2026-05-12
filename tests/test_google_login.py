"""Tests for Google login XPath helpers."""

from __future__ import annotations

from automated_extraction.chatgpt_auth.google_login import xpath_literal


class TestXpathLiteral:
    def test_simple_string_uses_single_quotes(self):
        assert xpath_literal("hello") == "'hello'"

    def test_string_with_single_quote_uses_double_quotes(self):
        assert xpath_literal("it's") == '"it\'s"'

    def test_string_with_double_quote_uses_single_quotes(self):
        assert xpath_literal('say "hi"') == "'say \"hi\"'"

    def test_string_with_both_quote_types_uses_concat(self):
        result = xpath_literal('it\'s a "test"')
        assert result.startswith("concat(")
        # Must be valid XPath-ish concat expression
        assert "it" in result
        assert "test" in result

    def test_email_address(self):
        result = xpath_literal("dev@zebora.io")
        assert "dev@zebora.io" in result

    def test_empty_string(self):
        result = xpath_literal("")
        assert result == "''"
