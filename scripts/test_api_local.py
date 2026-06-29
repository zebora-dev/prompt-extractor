#!/usr/bin/env python3
"""Local smoke test for LLM API extraction."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from automated_extraction.config import Settings
from automated_extraction.extraction import run_api_extraction_job


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test LLM API extraction locally.")
    parser.add_argument("--model", default="gpt-4o", help="Model name (default: gpt-4o)")
    parser.add_argument("--use-web-search", action="store_true", help="Enable native web search tool")
    parser.add_argument("--batch-id", help="BrandSight batch UUID")
    parser.add_argument("--brand-id", help="Brand UUID override")
    parser.add_argument("--limit", type=int, default=1, help="Max prompts to run (default: 1)")
    parser.add_argument("--measurements-filter", help="Filter prompts by measurements field")
    parser.add_argument("--force-rerun", action="store_true", help="Re-run even if output already exists")
    args = parser.parse_args()

    settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)

    print(f"\n=== LLM API Extraction Test ===")
    print(f"  model       : {args.model}")
    print(f"  web_search  : {args.use_web_search}")
    print(f"  batch_id    : {args.batch_id or '<none>'}")
    print(f"  limit       : {args.limit}")
    print()

    result = run_api_extraction_job(
        settings=settings,
        batch_id=args.batch_id,
        brand_id=args.brand_id,
        limit=args.limit,
        model_name=args.model,
        use_web_search=args.use_web_search,
        force_rerun=args.force_rerun,
        measurements_filter=args.measurements_filter,
    )

    print(f"\n=== Result ===")
    print(f"  saved_count   : {result.saved_count}")
    print(f"  skipped_count : {result.skipped_count}")
    print(f"  failed_count  : {result.failed_count}")
    if result.saved_outputs:
        out = result.saved_outputs[0]
        print(f"\n  First saved output:")
        print(f"    llm_model : {out.get('llm_model')}")
        print(f"    sources   : {len(out.get('sources') or [])} source(s)")
        meta = (out.get("output_metadata") or {}).get("original_metadata") or {}
        if meta:
            print(f"    tokens    : {meta.get('token_usage')}")
            print(f"    latency   : {meta.get('latency_ms')}ms")
            print(f"    trace_url : {meta.get('langfuse_trace_url')}")
            if meta.get("web_search_queries"):
                print(f"    search queries: {meta['web_search_queries']}")


if __name__ == "__main__":
    main()
