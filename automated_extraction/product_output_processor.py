from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .api_client import ApiClient
from .config import Settings
from .prompt_output_processor import html_to_markdown

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductOutputProcessResult:
    status: str
    processed_count: int
    saved_count: int
    skipped_count: int
    failed_count: int
    failures: list[dict[str, Any]]


def process_product_outputs(
    *,
    settings: Settings,
    product_output_refs: list[dict[str, Any]] | None = None,
) -> ProductOutputProcessResult:
    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )
    refs = product_output_refs or []
    LOGGER.info("Starting product output persistence. output_refs=%s", len(refs))

    processed_count = 0
    saved_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []

    for ref in refs:
        output_id = ref.get("output_id") or ref.get("id") or ref.get("prompt_output_id")
        prompt_id = ref.get("prompt_id")
        brand_id = ref.get("brand_id")
        batch_id = ref.get("batch_id")
        products = ref.get("products") if isinstance(ref.get("products"), list) else []

        if not output_id or not prompt_id or not brand_id or not batch_id:
            skipped_count += len(products) or 1
            LOGGER.warning(
                "Skipping product output ref missing required identifiers. output_id=%s prompt_id=%s brand_id=%s batch_id=%s products=%s",
                output_id,
                prompt_id,
                brand_id,
                batch_id,
                len(products),
            )
            continue

        rows: list[dict[str, Any]] = []
        for product in products:
            processed_count += 1
            if not isinstance(product, dict):
                skipped_count += 1
                continue

            try:
                rows.append(
                    build_product_row(
                        product, output_id=output_id, prompt_id=prompt_id, brand_id=brand_id, batch_id=batch_id
                    )
                )
            except Exception as exc:
                failed_count += 1
                failure = {
                    "output_id": output_id,
                    "prompt_id": prompt_id,
                    "button_index": product.get("button_index"),
                    "error": str(exc),
                }
                failures.append(failure)
                LOGGER.exception(
                    "Failed to prepare product row. output_id=%s prompt_id=%s button_index=%s: %s",
                    output_id,
                    prompt_id,
                    product.get("button_index"),
                    exc,
                )

        if not rows:
            continue

        try:
            saved_rows = api.save_prompt_output_products(rows)
            saved_count += len(saved_rows)
            LOGGER.info(
                "Saved product output rows. output_id=%s prompt_id=%s prepared=%s saved=%s",
                output_id,
                prompt_id,
                len(rows),
                len(saved_rows),
            )
        except Exception as exc:
            failed_count += len(rows)
            failure = {"output_id": output_id, "prompt_id": prompt_id, "error": str(exc)}
            failures.append(failure)
            LOGGER.exception(
                "Failed to save product output rows. output_id=%s prompt_id=%s: %s", output_id, prompt_id, exc
            )

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return ProductOutputProcessResult(
        status=status,
        processed_count=processed_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        failures=failures,
    )


def build_product_row(
    product: dict[str, Any],
    *,
    output_id: int | str,
    prompt_id: str,
    brand_id: str,
    batch_id: str,
) -> dict[str, Any]:
    raw_html = str(product.get("raw_html") or "")
    markdown = html_to_markdown(raw_html) if raw_html else ""
    images = product.get("images") if isinstance(product.get("images"), list) else []
    links = product.get("links") if isinstance(product.get("links"), list) else []
    text_length = product.get("text_length")
    if text_length is None:
        text_length = len(markdown)

    return {
        "output_id": int(output_id),
        "prompt_id": str(prompt_id),
        "brand_id": str(brand_id),
        "batch_id": str(batch_id),
        "raw_html": raw_html,
        "markdown": markdown,
        "links": links,
        "images": images,
        "html_length": int(product.get("html_length") or len(raw_html)),
        "image_count": int(product.get("image_count") or len(images)),
        "text_length": int(text_length or 0),
        "button_index": int(product.get("button_index") or product.get("index") or 0),
        "capture_method": str(product.get("capture_method") or "unknown"),
        "created_at": datetime.now(UTC).isoformat(),
    }
