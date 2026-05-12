"""Tests for batch flow parameter logic (no Prefect server required)."""

from __future__ import annotations

import math


def _calculate_run_count(remaining_count: int, skip: int, limit: int) -> int:
    """Mirrors the run_count calculation in prompt_extraction_batch_flow."""
    effective = max(0, remaining_count - skip)
    return math.ceil(effective / limit) if effective else 0


def _run_skips(skip: int, limit: int, run_count: int) -> list[int]:
    """Mirrors the skip applied to each sub-run."""
    return [skip if i == 1 else 0 for i in range(1, run_count + 1)]


class TestRunCountCalculation:
    def test_exact_division(self):
        assert _calculate_run_count(10, 0, 5) == 2

    def test_rounds_up(self):
        assert _calculate_run_count(11, 0, 5) == 3

    def test_skip_reduces_effective_count(self):
        # 34 remaining, skip 5 → 29 effective → ceil(29/5) = 6
        assert _calculate_run_count(34, 5, 5) == 6

    def test_skip_exceeds_remaining(self):
        assert _calculate_run_count(5, 10, 5) == 0

    def test_zero_remaining_returns_zero(self):
        assert _calculate_run_count(0, 0, 5) == 0

    def test_single_prompt(self):
        assert _calculate_run_count(1, 0, 5) == 1


class TestSubRunSkips:
    def test_skip_only_on_first_run(self):
        skips = _run_skips(skip=10, limit=5, run_count=3)
        assert skips == [10, 0, 0]

    def test_no_skip_all_zeros(self):
        skips = _run_skips(skip=0, limit=5, run_count=3)
        assert skips == [0, 0, 0]

    def test_single_run_uses_skip(self):
        skips = _run_skips(skip=20, limit=5, run_count=1)
        assert skips == [20]

    def test_skip_not_cumulative(self):
        # Ensure we reverted the cumulative skip bug
        skips = _run_skips(skip=5, limit=5, run_count=4)
        assert skips[0] == 5
        assert all(s == 0 for s in skips[1:])
