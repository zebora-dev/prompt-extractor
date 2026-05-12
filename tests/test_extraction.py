"""Tests for extraction.py pure logic (no Chrome or Supabase required)."""

from __future__ import annotations

import pytest

from automated_extraction.extraction import (
    first_brand_id,
    prompt_text,
    with_brand_id,
)


class TestPromptText:
    def test_reads_text_key(self):
        assert prompt_text({"text": "Hello world", "id": "1"}) == "Hello world"

    def test_reads_prompt_key_as_fallback(self):
        assert prompt_text({"prompt": "Fallback prompt", "id": "1"}) == "Fallback prompt"

    def test_text_key_takes_priority_over_prompt(self):
        assert prompt_text({"text": "Primary", "prompt": "Secondary"}) == "Primary"

    def test_missing_text_raises(self):
        with pytest.raises(RuntimeError, match="Prompt missing text"):
            prompt_text({"id": "1"})

    def test_empty_text_raises(self):
        with pytest.raises(RuntimeError, match="Prompt missing text"):
            prompt_text({"text": "", "id": "1"})


class TestFirstBrandId:
    def test_returns_first_brand_id(self):
        prompts = [
            {"id": "1", "brand_id": "brand-abc"},
            {"id": "2", "brand_id": "brand-xyz"},
        ]
        assert first_brand_id(prompts) == "brand-abc"

    def test_skips_prompts_without_brand_id(self):
        prompts = [
            {"id": "1"},
            {"id": "2", "brand_id": "brand-abc"},
        ]
        assert first_brand_id(prompts) == "brand-abc"

    def test_returns_none_when_no_brand_id(self):
        assert first_brand_id([{"id": "1"}, {"id": "2"}]) is None

    def test_empty_list_returns_none(self):
        assert first_brand_id([]) is None


class TestWithBrandId:
    def test_adds_brand_id_when_missing(self):
        result = with_brand_id({"id": "1", "text": "Hi"}, "brand-abc")
        assert result["brand_id"] == "brand-abc"

    def test_preserves_existing_brand_id(self):
        result = with_brand_id({"id": "1", "brand_id": "original"}, "brand-new")
        assert result["brand_id"] == "original"

    def test_returns_original_when_no_brand_id_provided(self):
        prompt = {"id": "1", "text": "Hi"}
        result = with_brand_id(prompt, None)
        assert result is prompt

    def test_does_not_mutate_original(self):
        prompt = {"id": "1", "text": "Hi"}
        with_brand_id(prompt, "brand-abc")
        assert "brand_id" not in prompt
