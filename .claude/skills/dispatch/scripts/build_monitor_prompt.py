#!/usr/bin/env python3
"""Build a portable /dispatch monitor prompt from explicit state."""

from __future__ import annotations

import argparse


def add_if_present(parts: list[str], key: str, value: str | None) -> None:
    if value:
        parts.append(f"{key}={value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--flow-runs", required=True, help="Comma-separated flow run IDs")
    parser.add_argument("--machines", required=True, help="Comma-separated Fly machine IDs")
    parser.add_argument("--worker-count", required=True)
    parser.add_argument("--extraction-type", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--work-pool", required=True)
    parser.add_argument("--required-models")
    parser.add_argument("--prev-output-counts")
    parser.add_argument("--zero-output-accounts")
    parser.add_argument("--prev-model-counts")
    parser.add_argument("--consecutive-zero-replacements")
    args = parser.parse_args()

    parts = [
        "/dispatch --monitor",
        f"batch_id={args.batch_id}",
        f"flow_runs={args.flow_runs}",
        f"machines={args.machines}",
        f"worker_count={args.worker_count}",
        f"extraction_type={args.extraction_type}",
        f"deployment_id={args.deployment_id}",
        f"app={args.app}",
        f"work_pool={args.work_pool}",
    ]
    add_if_present(parts, "required_models", args.required_models)
    add_if_present(parts, "prev_output_counts", args.prev_output_counts)
    add_if_present(parts, "zero_output_accounts", args.zero_output_accounts)
    add_if_present(parts, "prev_model_counts", args.prev_model_counts)
    add_if_present(
        parts,
        "consecutive_zero_replacements",
        args.consecutive_zero_replacements,
    )
    print(" ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
