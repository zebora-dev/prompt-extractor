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
class EntityOutputProcessResult:
    status: str
    processed_count: int
    saved_count: int
    skipped_count: int
    failed_count: int
    failures: list[dict[str, Any]]


def process_entity_outputs(
    *,
    settings: Settings,
    entity_output_refs: list[dict[str, Any]] | None = None,
) -> EntityOutputProcessResult:
    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )
    refs = entity_output_refs or []
    LOGGER.info("Starting entity output persistence. output_refs=%s", len(refs))

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
        entities = ref.get("entities") if isinstance(ref.get("entities"), list) else []

        if not output_id or not prompt_id or not brand_id or not batch_id:
            skipped_count += len(entities) or 1
            LOGGER.warning(
                "Skipping entity output ref missing required identifiers. output_id=%s prompt_id=%s brand_id=%s batch_id=%s entities=%s",
                output_id,
                prompt_id,
                brand_id,
                batch_id,
                len(entities),
            )
            continue

        rows: list[dict[str, Any]] = []
        for entity in entities:
            processed_count += 1
            if not isinstance(entity, dict):
                skipped_count += 1
                continue

            try:
                rows.append(
                    build_entity_row(
                        entity, output_id=output_id, prompt_id=prompt_id, brand_id=brand_id, batch_id=batch_id
                    )
                )
            except Exception as exc:
                failed_count += 1
                failure = {
                    "output_id": output_id,
                    "prompt_id": prompt_id,
                    "entity_index": entity.get("entity_index"),
                    "entity_text": entity.get("entity_text"),
                    "error": str(exc),
                }
                failures.append(failure)
                LOGGER.exception(
                    "Failed to prepare entity row. output_id=%s prompt_id=%s entity_index=%s entity_text=%r: %s",
                    output_id,
                    prompt_id,
                    entity.get("entity_index"),
                    entity.get("entity_text"),
                    exc,
                )

        if not rows:
            continue

        try:
            saved_rows = api.save_prompt_output_entities(rows)
            saved_count += len(saved_rows)
            LOGGER.info(
                "Saved entity output rows. output_id=%s prompt_id=%s prepared=%s saved=%s",
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
                "Failed to save entity output rows. output_id=%s prompt_id=%s: %s", output_id, prompt_id, exc
            )

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return EntityOutputProcessResult(
        status=status,
        processed_count=processed_count,
        saved_count=saved_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        failures=failures,
    )


def build_entity_row(
    entity: dict[str, Any],
    *,
    output_id: int | str,
    prompt_id: str,
    brand_id: str,
    batch_id: str,
) -> dict[str, Any]:
    raw_html = str(entity.get("raw_html") or "")
    markdown = html_to_markdown(raw_html) if raw_html else ""
    images = entity.get("images") if isinstance(entity.get("images"), list) else []
    links = entity.get("links") if isinstance(entity.get("links"), list) else []
    text_length = entity.get("text_length")
    if text_length is None:
        text_length = len(markdown)

    return {
        "output_id": int(output_id),
        "prompt_id": str(prompt_id),
        "brand_id": str(brand_id),
        "batch_id": str(batch_id),
        "entity_text": str(entity.get("entity_text") or ""),
        "title": str(entity.get("title") or ""),
        "raw_html": raw_html,
        "markdown": markdown,
        "links": links,
        "images": images,
        "html_length": int(entity.get("html_length") or len(raw_html)),
        "image_count": int(entity.get("image_count") or len(images)),
        "text_length": int(text_length or 0),
        "entity_index": int(entity.get("entity_index") or entity.get("index") or 0),
        "capture_method": str(entity.get("capture_method") or "unknown"),
        "created_at": datetime.now(UTC).isoformat(),
    }
