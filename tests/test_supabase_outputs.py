"""Tests for supabase_prompt_outputs.py pure data-mapping logic."""

from __future__ import annotations

from automated_extraction.supabase_prompt_outputs import (
    PROMPT_OUTPUT_COLUMNS,
    PROMPT_OUTPUT_INSERT_COLUMNS,
    SupabasePromptOutputRepository,
    compact_row,
    model_filter_matches_required_models,
    output_to_row,
    row_to_output,
)


class TestCompactRow:
    def test_removes_none_values(self):
        row = {"prompt_id": "p1", "response": None, "markdown": "## Hi"}
        result = compact_row(row, ("prompt_id", "response", "markdown"))
        assert "response" not in result
        assert result["prompt_id"] == "p1"
        assert result["markdown"] == "## Hi"

    def test_removes_columns_not_in_allowed(self):
        row = {"prompt_id": "p1", "unknown_col": "value"}
        result = compact_row(row, ("prompt_id",))
        assert "unknown_col" not in result

    def test_empty_row_returns_empty(self):
        assert compact_row({}, PROMPT_OUTPUT_COLUMNS) == {}

    def test_preserves_falsy_non_none_values(self):
        row = {"prompt_id": "p1", "markdown": "", "sources": []}
        # empty string and list are falsy but not None — behaviour: compact_row
        # only strips None, not empty strings/lists
        result = compact_row(row, ("prompt_id", "markdown", "sources"))
        assert "markdown" in result
        assert "sources" in result


class TestOutputToRow:
    BASE_OUTPUT = {
        "id": 42,
        "prompt_id": "prompt-1",
        "brand_id": "brand-1",
        "batch_id": "batch-1",
        "response": "Some response",
        "markdown": "## Response",
        "llm_model": "gpt-4o",
        "run_at": "2026-05-08T10:00:00Z",
        "version_info": {"app_type": "automated_extraction"},
        "output_metadata": {"worker_name": "48e062ec746408"},
    }

    def test_maps_core_fields(self):
        row = output_to_row(self.BASE_OUTPUT, include_id=True)
        assert row["prompt_id"] == "prompt-1"
        assert row["brand_id"] == "brand-1"
        assert row["batch_id"] == "batch-1"
        assert row["llm_model"] == "gpt-4o"

    def test_include_id_false_excludes_id(self):
        row = output_to_row(self.BASE_OUTPUT, include_id=False)
        assert "id" not in row

    def test_include_id_true_includes_id(self):
        row = output_to_row(self.BASE_OUTPUT, include_id=True)
        assert row.get("id") == 42

    def test_metadata_mapped_from_output_metadata(self):
        row = output_to_row(self.BASE_OUTPUT, include_id=False)
        assert row.get("metadata") == {"worker_name": "48e062ec746408"}

    def test_none_fields_excluded(self):
        output = {**self.BASE_OUTPUT, "raw_html": None, "sources": None}
        row = output_to_row(output, include_id=False)
        assert "raw_html" not in row
        assert "sources" not in row

    def test_only_allowed_columns_present(self):
        row = output_to_row(self.BASE_OUTPUT, include_id=False)
        for key in row:
            assert key in PROMPT_OUTPUT_INSERT_COLUMNS, f"Unexpected column: {key}"


class TestRowToOutput:
    def test_maps_back_to_output_dict(self):
        row = {
            "id": 99,
            "prompt_id": "p1",
            "brand_id": "b1",
            "batch_id": "ba1",
            "response": "text",
            "llm_model": "gpt-4o",
            "run_at": "2026-05-08T10:00:00Z",
        }
        output = row_to_output(row)
        assert output["prompt_id"] == "p1"
        assert output["brand_id"] == "b1"
        assert output["llm_model"] == "gpt-4o"

    def test_missing_fields_handled_gracefully(self):
        row = {"prompt_id": "p1", "brand_id": "b1", "batch_id": "ba1"}
        output = row_to_output(row)
        assert output["prompt_id"] == "p1"


class TestRequiredModelFiltering:
    def test_broad_gpt_filter_matches_required_gpt_models(self):
        assert model_filter_matches_required_models("gpt", ["gpt-5-5", "gpt-5-3-mini"])

    def test_google_ai_overview_filter_does_not_match_required_gpt_models(self):
        assert not model_filter_matches_required_models(
            "google-ai-overview",
            ["gpt-5-5", "gpt-5-3-mini"],
        )

    def test_unrelated_filter_uses_own_model_outputs_not_required_model_completion(self):
        class FakeRepository(SupabasePromptOutputRepository):
            def _completed_output_ids(self, *, batch_id, brand_id, llm_model_filter):
                assert batch_id == "batch-1"
                assert brand_id == "brand-1"
                assert llm_model_filter == "google-ai-overview"
                return {"google-done"}

            def _active_claimed_ids(self, *, batch_id, llm_model_filter):
                assert batch_id == "batch-1"
                assert llm_model_filter == "google-ai-overview"
                return {"google-claimed"}

        repo = FakeRepository.__new__(FakeRepository)

        assert repo.completed_prompt_ids(
            batch_id="batch-1",
            brand_id="brand-1",
            llm_model_filter="google-ai-overview",
            required_models=["gpt-5-5", "gpt-5-3-mini"],
        ) == {"google-done", "google-claimed"}
