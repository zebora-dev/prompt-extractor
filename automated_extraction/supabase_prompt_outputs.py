from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any

from supabase import Client, create_client

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchRow:
    id: str
    name: str | None = None
    brand_id: str | None = None
    batch_type: str | None = None
    batch_metadata: Any = None
    config: Any = None
    dashboard_type: str | None = None
    dashboard_version: str | None = None
    description: str | None = None
    status: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    date: str | None = None
    created_by: str | None = None
    is_active: bool | None = None
    is_approved: str | None = None
    multi_llm: bool | None = None
    llm_models: Any = None
    brand: Any = None


@dataclass(frozen=True)
class PromptRow:
    id: str
    brand_id: str
    text: str
    active: bool | None = None
    approved: bool | None = None
    category: str | None = None
    created_at: str | None = None
    flag: bool | None = None
    measurements: Any = None
    metadata: Any = None
    tags: Any = None
    updated_at: str | None = None
    brand: Any = None


@dataclass(frozen=True)
class PromptOutputRow:
    batch_id: str
    brand_id: str
    prompt_id: str
    id: int | None = None
    response: str | None = None
    markdown: str | None = None
    raw_html: str | None = None
    sources: Any = None
    llm_model: str | None = None
    config: Any = None
    metadata: Any = None
    version_info: Any = None
    run_at: str | None = None


@dataclass(frozen=True)
class PromptOutputProductRow:
    output_id: int
    brand_id: str
    batch_id: str
    prompt_id: str
    id: int | None = None
    raw_html: str | None = None
    markdown: str | None = None
    links: Any = None
    images: Any = None
    html_length: int | None = None
    image_count: int | None = None
    text_length: int | None = None
    button_index: int | None = None
    capture_method: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class PromptOutputEntityRow:
    output_id: int
    brand_id: str
    batch_id: str
    prompt_id: str
    id: int | None = None
    entity_text: str | None = None
    title: str | None = None
    raw_html: str | None = None
    markdown: str | None = None
    links: Any = None
    images: Any = None
    html_length: int | None = None
    image_count: int | None = None
    text_length: int | None = None
    entity_index: int | None = None
    capture_method: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class PromptOutputSuggestionRow:
    output_id: int
    prompt_id: str
    brand_id: str
    batch_id: str
    index: int
    text: str
    id: int | None = None
    response: str | None = None
    sources: Any = None
    raw_html: str | None = None
    llm_model: str | None = None
    capture_method: str | None = None
    error: str | None = None
    metadata: Any = None
    created_at: str | None = None


BatchDict = dict[str, Any]
PromptDict = dict[str, Any]
PromptOutputDict = dict[str, Any]
PromptOutputProductDict = dict[str, Any]
PromptOutputEntityDict = dict[str, Any]
PromptOutputSuggestionDict = dict[str, Any]

BATCH_COLUMNS = tuple(field.name for field in fields(BatchRow) if field.name != "brand")
PROMPT_COLUMNS = tuple(field.name for field in fields(PromptRow) if field.name != "brand")
PROMPT_OUTPUT_COLUMNS = tuple(field.name for field in fields(PromptOutputRow))
PROMPT_OUTPUT_INSERT_COLUMNS = tuple(column for column in PROMPT_OUTPUT_COLUMNS if column != "id")
PROMPT_OUTPUT_UPDATE_COLUMNS = tuple(
    column for column in PROMPT_OUTPUT_COLUMNS if column not in {"id", "batch_id", "brand_id", "prompt_id"}
)
PROMPT_OUTPUT_PRODUCT_COLUMNS = tuple(field.name for field in fields(PromptOutputProductRow))
PROMPT_OUTPUT_PRODUCT_INSERT_COLUMNS = tuple(column for column in PROMPT_OUTPUT_PRODUCT_COLUMNS if column != "id")
PROMPT_OUTPUT_ENTITY_COLUMNS = tuple(field.name for field in fields(PromptOutputEntityRow))
PROMPT_OUTPUT_ENTITY_INSERT_COLUMNS = tuple(column for column in PROMPT_OUTPUT_ENTITY_COLUMNS if column != "id")
PROMPT_OUTPUT_SUGGESTION_COLUMNS = tuple(field.name for field in fields(PromptOutputSuggestionRow))
PROMPT_OUTPUT_SUGGESTION_INSERT_COLUMNS = tuple(column for column in PROMPT_OUTPUT_SUGGESTION_COLUMNS if column != "id")


@dataclass(frozen=True)
class SupabasePromptOutputRepository:
    supabase_url: str
    anon_key: str
    table_name: str = "prompts_outputs"
    product_table_name: str = "prompts_outputs_products"
    entity_table_name: str = "prompts_outputs_entities"
    suggestion_table_name: str = "prompts_outputs_suggestions"

    def __post_init__(self) -> None:
        if not self.supabase_url:
            raise RuntimeError("Missing Supabase URL for prompt output repository")
        if not self.anon_key:
            raise RuntimeError("Missing Supabase anon key for prompt output repository")

    @property
    def client(self) -> Client:
        if not hasattr(self, "_client"):
            object.__setattr__(self, "_client", create_client(self.supabase_url, self.anon_key))
        return getattr(self, "_client")

    def get_batches(self) -> list[BatchDict]:
        response = (
            self.client.table("batches")
            .select("*, brand:brands(name, description)")
            .order("started_at", desc=True)
            .execute()
        )
        return [row_to_batch(row) for row in response.data or [] if isinstance(row, dict)]

    def get_batch(self, batch_id: str) -> BatchDict:
        response = (
            self.client.table("batches")
            .select("*, brand:brands(name, description)")
            .eq("id", batch_id)
            .limit(1)
            .execute()
        )
        if response.data:
            return row_to_batch(response.data[0])
        raise RuntimeError(f"Batch not found: {batch_id}")

    def get_prompts(
        self,
        batch_id: str,
        brand_id: str,
        limit: int = 10000,
        *,
        only_remaining: bool = True,
        llm_model_filter: str | None = "gpt",
        required_models: list[str] | None = None,
    ) -> list[PromptDict]:
        response = (
            self.client.table("prompts")
            .select("*, brand:brands(name, description)")
            .eq("brand_id", brand_id)
            .eq("active", True)
            .order("created_at", desc=True)
            .limit(max(1, limit))
            .execute()
        )
        prompts = [
            row_to_prompt(row, batch_id=batch_id, brand_id=brand_id)
            for row in response.data or []
            if isinstance(row, dict)
        ]
        if not only_remaining:
            LOGGER.info("Loaded %s active prompt(s) for brand_id=%s without remaining filter.", len(prompts), brand_id)
            return prompts

        completed_ids = self.completed_prompt_ids(
            batch_id=batch_id,
            brand_id=brand_id,
            llm_model_filter=llm_model_filter,
            required_models=required_models,
        )
        remaining = [prompt for prompt in prompts if prompt.get("id") not in completed_ids]
        LOGGER.info(
            "Remaining prompts analysis. total_prompts=%s completed_count=%s remaining_count=%s "
            "batch_id=%s brand_id=%s llm_model_filter=%s required_models=%s",
            len(prompts),
            len(completed_ids),
            len(remaining),
            batch_id,
            brand_id,
            llm_model_filter or "any",
            required_models or "none",
        )
        return remaining

    def completed_prompt_ids(
        self,
        *,
        batch_id: str,
        brand_id: str,
        llm_model_filter: str | None = "gpt",
        required_models: list[str] | None = None,
    ) -> set[str]:
        """Return prompt_ids that are fully complete for this batch.

        When ``required_models`` is provided, a prompt is only considered
        complete if it has outputs for *all* listed models (exact match).
        When absent, falls back to the existing ILIKE behaviour on
        ``llm_model_filter``.
        """
        if required_models:
            # Per-model exact-match sets; intersection = prompts with ALL models.
            per_model_sets: list[set[str]] = []
            for model in required_models:
                resp = (
                    self.client.table(self.table_name)
                    .select("prompt_id")
                    .eq("batch_id", batch_id)
                    .eq("brand_id", brand_id)
                    .eq("active", True)
                    .eq("llm_model", model)
                    .limit(10000)
                    .execute()
                )
                ids = {
                    str(r.get("prompt_id"))
                    for r in resp.data or []
                    if isinstance(r, dict) and r.get("prompt_id")
                }
                per_model_sets.append(ids)
                LOGGER.debug("required_models: model=%s found=%s batch_id=%s", model, len(ids), batch_id)

            complete = per_model_sets[0].intersection(*per_model_sets[1:]) if per_model_sets else set()
            LOGGER.info(
                "required_models completion. models=%s fully_complete=%s batch_id=%s brand_id=%s",
                required_models,
                len(complete),
                batch_id,
                brand_id,
            )
            # Also exclude any prompts currently claimed by another worker.
            return complete | self._active_claimed_ids(batch_id=batch_id, llm_model_filter=llm_model_filter)

        # Original behaviour: ILIKE filter on llm_model_filter.
        done_ids = self._completed_output_ids(batch_id=batch_id, brand_id=brand_id, llm_model_filter=llm_model_filter)
        return done_ids | self._active_claimed_ids(batch_id=batch_id, llm_model_filter=llm_model_filter)

    def _completed_output_ids(
        self, *, batch_id: str, brand_id: str, llm_model_filter: str | None
    ) -> set[str]:
        query = (
            self.client.table(self.table_name)
            .select("prompt_id")
            .eq("batch_id", batch_id)
            .eq("brand_id", brand_id)
            .eq("active", True)
        )
        if llm_model_filter:
            query = query.ilike("llm_model", f"%{llm_model_filter}%")
        response = query.limit(10000).execute()
        return {
            str(row.get("prompt_id")) for row in response.data or [] if isinstance(row, dict) and row.get("prompt_id")
        }

    def _active_claimed_ids(self, *, batch_id: str, llm_model_filter: str | None) -> set[str]:
        """Return prompt_ids currently held by an active (non-expired) claim."""
        try:
            now_iso = datetime.now(UTC).isoformat()
            claims_query = (
                self.client.table("prompt_claims")
                .select("prompt_id")
                .eq("batch_id", batch_id)
                .eq("status", "pending")
                .gt("expires_at", now_iso)
            )
            if llm_model_filter:
                claims_query = claims_query.ilike("llm_model", f"%{llm_model_filter}%")
            claims_response = claims_query.limit(10000).execute()
            claimed_ids = {
                str(row.get("prompt_id"))
                for row in claims_response.data or []
                if isinstance(row, dict) and row.get("prompt_id")
            }
            if claimed_ids:
                LOGGER.info(
                    "completed_prompt_ids: %s prompt(s) excluded due to active claims. batch_id=%s llm_model_filter=%s",
                    len(claimed_ids),
                    batch_id,
                    llm_model_filter or "any",
                )
            return claimed_ids
        except Exception as exc:
            LOGGER.warning(
                "completed_prompt_ids: could not load active claims (table may not exist yet) — "
                "falling back to outputs-only. batch_id=%s error=%s",
                batch_id,
                exc,
            )
            return set()

    def try_claim_prompt(
        self,
        prompt_id: str,
        batch_id: str,
        brand_id: str,
        llm_model: str,
        worker_id: str,
        ttl_minutes: int = 20,
    ) -> bool:
        """Atomically claim a prompt for processing via the try_claim_prompt RPC.

        Returns True if this worker successfully holds the claim, False if another
        worker already has an active (pending, non-expired) claim on this prompt.

        Fails open (returns True) if the RPC is unavailable so that existing
        deployments without the migration still work — the concurrent-output
        check before saving acts as a secondary safety net in that case.
        """
        try:
            response = self.client.rpc(
                "try_claim_prompt",
                {
                    "p_prompt_id": str(prompt_id),
                    "p_batch_id": str(batch_id),
                    "p_brand_id": str(brand_id),
                    "p_llm_model": llm_model,
                    "p_worker_id": worker_id,
                    "p_ttl_minutes": ttl_minutes,
                },
            ).execute()
            return bool(response.data)
        except Exception as exc:
            LOGGER.warning(
                "try_claim_prompt RPC unavailable for prompt %s — failing open. "
                "Run docs/migrations/001_prompt_claims.sql to enable claiming. error=%s",
                prompt_id,
                exc,
            )
            return True  # Fail-open: allow processing; concurrent-output check is the fallback

    def release_claim(
        self,
        prompt_id: str,
        batch_id: str,
        llm_model: str,
        error_message: str | None = None,
    ) -> None:
        """Mark a claim as failed so the prompt is available for retry."""
        try:
            self.client.table("prompt_claims").update(
                {"status": "failed", "error_message": (error_message or "")[:1000]}
            ).eq("prompt_id", str(prompt_id)).eq("batch_id", str(batch_id)).eq("llm_model", llm_model).eq(
                "status", "pending"
            ).execute()
        except Exception as exc:
            LOGGER.warning("release_claim failed for prompt %s: %s", prompt_id, exc)

    def complete_claim(
        self,
        prompt_id: str,
        batch_id: str,
        llm_model: str,
    ) -> None:
        """Delete a claim after successful processing.

        The prompts_outputs record is the source of truth for completion — the
        claim row is no longer needed once the output is saved.
        """
        try:
            self.client.table("prompt_claims").delete().eq("prompt_id", str(prompt_id)).eq(
                "batch_id", str(batch_id)
            ).eq("llm_model", llm_model).execute()
        except Exception as exc:
            LOGGER.warning("complete_claim failed for prompt %s: %s", prompt_id, exc)

    def prompt_output_exists(
        self,
        prompt_id: str,
        brand_id: str,
        batch_id: str | None,
        *,
        llm_model_filter: str | None = "gpt",
    ) -> bool:
        return (
            self.find_existing_prompt_output(prompt_id, brand_id, batch_id, llm_model_filter=llm_model_filter)
            is not None
        )

    def find_existing_prompt_output(
        self,
        prompt_id: str,
        brand_id: str,
        batch_id: str | None,
        *,
        llm_model_filter: str | None = "gpt",
        required_models: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Return an existing output if the prompt is already fully complete.

        When ``required_models`` is set a prompt is only considered complete when
        ALL required models have an active output — otherwise return None so the
        prompt is processed again (for the missing model).
        """
        if not prompt_id or not brand_id or not batch_id:
            return None

        if required_models:
            # Check each required model — prompt is done only if ALL are present.
            for model in required_models:
                resp = (
                    self.client.table(self.table_name)
                    .select("id")
                    .eq("prompt_id", prompt_id)
                    .eq("brand_id", brand_id)
                    .eq("batch_id", batch_id)
                    .eq("active", True)
                    .eq("llm_model", model)
                    .limit(1)
                    .execute()
                )
                if not resp.data:
                    return None  # Missing this model — not yet complete
            # All required models present — return a sentinel so caller skips
            return {"llm_model": ",".join(required_models), "run_at": None, "id": None}

        query = (
            self.client.table(self.table_name)
            .select("id,prompt_id,brand_id,batch_id,llm_model,run_at")
            .eq("prompt_id", prompt_id)
            .eq("brand_id", brand_id)
            .eq("batch_id", batch_id)
            .eq("active", True)
        )
        if llm_model_filter:
            query = query.ilike("llm_model", f"%{llm_model_filter}%")

        response = query.order("id", desc=True).limit(1).execute()
        if not response.data:
            return None
        return row_to_output(response.data[0])

    def save_prompt_output(self, output: dict[str, Any]) -> dict[str, Any] | None:
        row = output_to_row(output, include_id=False)
        prompt_id = row.get("prompt_id")
        batch_id = row.get("batch_id")
        llm_model = row.get("llm_model")
        LOGGER.info(
            "Saving prompt output directly to Supabase. table=%s prompt_id=%s llm_model=%s",
            self.table_name,
            prompt_id,
            llm_model,
        )
        # Deactivate any existing active rows for this prompt+batch+model before inserting.
        if prompt_id and batch_id and llm_model:
            self.client.table(self.table_name).update({"active": False}).eq("prompt_id", prompt_id).eq(
                "batch_id", batch_id
            ).eq("llm_model", llm_model).eq("active", True).execute()
        response = self.client.table(self.table_name).insert(row).execute()
        if not response.data:
            return None
        return row_to_output(response.data[0])

    def save_prompt_output_products(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [product_to_row(product, include_id=False) for product in products]
        rows = [row for row in rows if row.get("output_id") and row.get("prompt_id") and row.get("brand_id")]
        if not rows:
            return []

        LOGGER.info(
            "Saving prompt output product rows directly to Supabase. table=%s count=%s",
            self.product_table_name,
            len(rows),
        )
        response = self.client.table(self.product_table_name).insert(rows).execute()
        return [row_to_product(row) for row in response.data or [] if isinstance(row, dict)]

    def save_prompt_output_entities(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [entity_to_row(entity, include_id=False) for entity in entities]
        rows = [row for row in rows if row.get("output_id") and row.get("prompt_id") and row.get("brand_id")]
        if not rows:
            return []

        LOGGER.info(
            "Saving prompt output entity rows directly to Supabase. table=%s count=%s",
            self.entity_table_name,
            len(rows),
        )
        response = self.client.table(self.entity_table_name).insert(rows).execute()
        return [row_to_entity(row) for row in response.data or [] if isinstance(row, dict)]

    def save_prompt_output_suggestions(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [suggestion_to_row(s, include_id=False) for s in suggestions]
        rows = [row for row in rows if row.get("output_id") and row.get("prompt_id") and row.get("brand_id")]
        if not rows:
            return []

        LOGGER.info(
            "Saving prompt output suggestion rows directly to Supabase. table=%s count=%s",
            self.suggestion_table_name,
            len(rows),
        )
        response = self.client.table(self.suggestion_table_name).insert(rows).execute()
        return [row_to_suggestion(row) for row in response.data or [] if isinstance(row, dict)]

    def get_prompt_output(self, output_id: int | str) -> dict[str, Any] | None:
        outputs = self.get_prompt_outputs(output_id=output_id, limit=1)
        return outputs[0] if outputs else None

    def get_prompt_outputs(
        self,
        *,
        output_id: int | str | None = None,
        batch_id: str | None = None,
        brand_id: str | None = None,
        prompt_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = self.client.table(self.table_name).select(",".join(PROMPT_OUTPUT_COLUMNS))
        if output_id:
            query = query.eq("id", output_id)
        if batch_id:
            query = query.eq("batch_id", batch_id)
        if brand_id:
            query = query.eq("brand_id", brand_id)
        if prompt_id:
            query = query.eq("prompt_id", prompt_id)

        response = query.order("id", desc=True).limit(max(1, limit)).execute()
        rows = response.data or []
        return [row_to_output(row) for row in rows if isinstance(row, dict)]

    def update_prompt_output(self, output: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any] | None:
        row_patch = patch_to_row(patch)
        if not row_patch:
            return None

        output_id = output.get("id") or output.get("output_id") or output.get("prompt_output_id")
        if output_id:
            response = self.client.table(self.table_name).update(row_patch).eq("id", output_id).execute()
        else:
            prompt_id = output.get("prompt_id")
            brand_id = output.get("brand_id")
            batch_id = output.get("batch_id")
            if not prompt_id or not brand_id or not batch_id:
                raise RuntimeError("Cannot update prompt output without id or prompt_id/brand_id/batch_id")
            response = (
                self.client.table(self.table_name)
                .update(row_patch)
                .eq("prompt_id", prompt_id)
                .eq("brand_id", brand_id)
                .eq("batch_id", batch_id)
                .execute()
            )

        if not response.data:
            return None
        return row_to_output(response.data[0])


def output_to_row(output: dict[str, Any], *, include_id: bool) -> dict[str, Any]:
    row = {
        "id": output.get("id") or output.get("output_id") or output.get("prompt_output_id"),
        "prompt_id": output.get("prompt_id"),
        "brand_id": output.get("brand_id"),
        "batch_id": output.get("batch_id"),
        "response": output.get("response"),
        "markdown": output.get("markdown"),
        "raw_html": output.get("raw_html"),
        "sources": output.get("sources"),
        "llm_model": output.get("llm_model"),
        "config": output.get("config"),
        "metadata": output.get("output_metadata", output.get("metadata")),
        "version_info": output.get("version_info"),
        "run_at": output.get("run_at"),
    }
    allowed_columns = PROMPT_OUTPUT_COLUMNS if include_id else PROMPT_OUTPUT_INSERT_COLUMNS
    return compact_row(row, allowed_columns)


def patch_to_row(patch: dict[str, Any]) -> dict[str, Any]:
    row = {
        "response": patch.get("response"),
        "markdown": patch.get("markdown"),
        "raw_html": patch.get("raw_html"),
        "sources": patch.get("sources"),
        "llm_model": patch.get("llm_model"),
        "config": patch.get("config"),
        "metadata": patch.get("output_metadata", patch.get("metadata")),
        "version_info": patch.get("version_info"),
        "run_at": patch.get("run_at"),
    }
    return compact_row(row, PROMPT_OUTPUT_UPDATE_COLUMNS)


def product_to_row(product: dict[str, Any], *, include_id: bool) -> dict[str, Any]:
    row = {
        "id": product.get("id"),
        "output_id": product.get("output_id"),
        "prompt_id": product.get("prompt_id"),
        "brand_id": product.get("brand_id"),
        "batch_id": product.get("batch_id"),
        "raw_html": product.get("raw_html"),
        "markdown": product.get("markdown"),
        "links": product.get("links"),
        "images": product.get("images"),
        "html_length": product.get("html_length"),
        "image_count": product.get("image_count"),
        "text_length": product.get("text_length"),
        "button_index": product.get("button_index"),
        "capture_method": product.get("capture_method"),
        "created_at": product.get("created_at"),
    }
    allowed_columns = PROMPT_OUTPUT_PRODUCT_COLUMNS if include_id else PROMPT_OUTPUT_PRODUCT_INSERT_COLUMNS
    return compact_row(row, allowed_columns)


def entity_to_row(entity: dict[str, Any], *, include_id: bool) -> dict[str, Any]:
    row = {
        "id": entity.get("id"),
        "output_id": entity.get("output_id"),
        "prompt_id": entity.get("prompt_id"),
        "brand_id": entity.get("brand_id"),
        "batch_id": entity.get("batch_id"),
        "entity_text": entity.get("entity_text"),
        "title": entity.get("title"),
        "raw_html": entity.get("raw_html"),
        "markdown": entity.get("markdown"),
        "links": entity.get("links"),
        "images": entity.get("images"),
        "html_length": entity.get("html_length"),
        "image_count": entity.get("image_count"),
        "text_length": entity.get("text_length"),
        "entity_index": entity.get("entity_index"),
        "capture_method": entity.get("capture_method"),
        "created_at": entity.get("created_at"),
    }
    allowed_columns = PROMPT_OUTPUT_ENTITY_COLUMNS if include_id else PROMPT_OUTPUT_ENTITY_INSERT_COLUMNS
    return compact_row(row, allowed_columns)


def compact_row(row: dict[str, Any], allowed_columns: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in allowed_columns and value is not None}


def row_to_batch(row: dict[str, Any]) -> BatchDict:
    typed_row = BatchRow(
        id=str(row.get("id") or ""),
        name=row.get("name"),
        brand_id=row.get("brand_id"),
        batch_type=row.get("batch_type"),
        batch_metadata=row.get("batch_metadata"),
        config=row.get("config"),
        dashboard_type=row.get("dashboard_type"),
        dashboard_version=row.get("dashboard_version"),
        description=row.get("description"),
        status=row.get("status"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        date=row.get("date"),
        created_by=row.get("created_by"),
        is_active=row.get("is_active"),
        is_approved=row.get("is_approved"),
        multi_llm=row.get("multi_llm"),
        llm_models=row.get("llm_models"),
        brand=row.get("brand"),
    )
    return asdict(typed_row)


def row_to_prompt(row: dict[str, Any], *, batch_id: str, brand_id: str) -> PromptDict:
    typed_row = PromptRow(
        id=str(row.get("id") or ""),
        brand_id=str(row.get("brand_id") or brand_id),
        text=str(row.get("text") or ""),
        active=row.get("active"),
        approved=row.get("approved"),
        category=row.get("category"),
        created_at=row.get("created_at"),
        flag=row.get("flag"),
        measurements=row.get("measurements"),
        metadata=row.get("metadata"),
        tags=row.get("tags"),
        updated_at=row.get("updated_at"),
        brand=row.get("brand"),
    )
    prompt = asdict(typed_row)
    prompt["batch_id"] = batch_id
    return prompt


def row_to_output(row: dict[str, Any]) -> PromptOutputDict:
    typed_row = PromptOutputRow(
        id=row.get("id"),
        prompt_id=str(row.get("prompt_id") or ""),
        brand_id=str(row.get("brand_id") or ""),
        batch_id=str(row.get("batch_id") or ""),
        response=row.get("response"),
        markdown=row.get("markdown"),
        raw_html=row.get("raw_html"),
        sources=row.get("sources"),
        llm_model=row.get("llm_model"),
        config=row.get("config"),
        metadata=row.get("metadata"),
        version_info=row.get("version_info"),
        run_at=row.get("run_at"),
    )
    payload = asdict(typed_row)
    payload["output_id"] = typed_row.id
    payload["output_metadata"] = (
        typed_row.metadata if isinstance(typed_row.metadata, dict) else typed_row.metadata or {}
    )
    return payload


def row_to_product(row: dict[str, Any]) -> PromptOutputProductDict:
    typed_row = PromptOutputProductRow(
        id=row.get("id"),
        output_id=int(row.get("output_id") or 0),
        prompt_id=str(row.get("prompt_id") or ""),
        brand_id=str(row.get("brand_id") or ""),
        batch_id=str(row.get("batch_id") or ""),
        raw_html=row.get("raw_html"),
        markdown=row.get("markdown"),
        links=row.get("links"),
        images=row.get("images"),
        html_length=row.get("html_length"),
        image_count=row.get("image_count"),
        text_length=row.get("text_length"),
        button_index=row.get("button_index"),
        capture_method=row.get("capture_method"),
        created_at=row.get("created_at"),
    )
    return asdict(typed_row)


def row_to_entity(row: dict[str, Any]) -> PromptOutputEntityDict:
    typed_row = PromptOutputEntityRow(
        id=row.get("id"),
        output_id=int(row.get("output_id") or 0),
        prompt_id=str(row.get("prompt_id") or ""),
        brand_id=str(row.get("brand_id") or ""),
        batch_id=str(row.get("batch_id") or ""),
        entity_text=row.get("entity_text"),
        title=row.get("title"),
        raw_html=row.get("raw_html"),
        markdown=row.get("markdown"),
        links=row.get("links"),
        images=row.get("images"),
        html_length=row.get("html_length"),
        image_count=row.get("image_count"),
        text_length=row.get("text_length"),
        entity_index=row.get("entity_index"),
        capture_method=row.get("capture_method"),
        created_at=row.get("created_at"),
    )
    return asdict(typed_row)


def suggestion_to_row(suggestion: dict[str, Any], *, include_id: bool) -> dict[str, Any]:
    row = {
        "id": suggestion.get("id"),
        "output_id": suggestion.get("output_id"),
        "prompt_id": suggestion.get("prompt_id"),
        "brand_id": suggestion.get("brand_id"),
        "batch_id": suggestion.get("batch_id"),
        "index": suggestion.get("index"),
        "text": suggestion.get("text"),
        "response": suggestion.get("response"),
        "sources": suggestion.get("sources"),
        "raw_html": suggestion.get("raw_html"),
        "llm_model": suggestion.get("llm_model"),
        "capture_method": suggestion.get("capture_method"),
        "error": suggestion.get("error"),
        "metadata": suggestion.get("metadata"),
        "created_at": suggestion.get("created_at"),
    }
    allowed_columns = PROMPT_OUTPUT_SUGGESTION_COLUMNS if include_id else PROMPT_OUTPUT_SUGGESTION_INSERT_COLUMNS
    return compact_row(row, allowed_columns)


def row_to_suggestion(row: dict[str, Any]) -> PromptOutputSuggestionDict:
    typed_row = PromptOutputSuggestionRow(
        id=row.get("id"),
        output_id=int(row.get("output_id") or 0),
        prompt_id=str(row.get("prompt_id") or ""),
        brand_id=str(row.get("brand_id") or ""),
        batch_id=str(row.get("batch_id") or ""),
        index=int(row.get("index") or 0),
        text=str(row.get("text") or ""),
        response=row.get("response"),
        sources=row.get("sources"),
        raw_html=row.get("raw_html"),
        llm_model=row.get("llm_model"),
        capture_method=row.get("capture_method"),
        error=row.get("error"),
        metadata=row.get("metadata"),
        created_at=row.get("created_at"),
    )
    return asdict(typed_row)
