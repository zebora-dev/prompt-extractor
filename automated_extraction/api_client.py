from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .supabase_prompt_outputs import SupabasePromptOutputRepository


@dataclass(frozen=True)
class ApiClient:
    base_url: str
    anon_key: str
    supabase_url: str | None = None
    prompt_outputs_table: str = "prompts_outputs"
    prompt_output_products_table: str = "prompts_outputs_products"
    prompt_output_entities_table: str = "prompts_outputs_entities"
    prompt_output_suggestions_table: str = "prompts_outputs_suggestions"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.anon_key}",
            "apikey": self.anon_key,
            "x-client-info": "brandsight-automated-extraction/1.0.0",
        }

    def get_batches(self) -> list[dict[str, Any]]:
        return self.supabase.get_batches()

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self.supabase.get_batch(batch_id)

    def get_prompts(
        self,
        batch_id: str,
        brand_id: str,
        limit: int = 10000,
        *,
        only_remaining: bool = True,
        llm_model_filter: str | None = "gpt",
        required_models: list[str] | None = None,
        measurements_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.supabase.get_prompts(
            batch_id,
            brand_id,
            limit,
            only_remaining=only_remaining,
            llm_model_filter=llm_model_filter,
            required_models=required_models,
            measurements_filter=measurements_filter,
        )

    def prompt_output_exists(
        self,
        prompt_id: str,
        brand_id: str,
        batch_id: str | None,
        *,
        llm_model_filter: str | None = "gpt",
    ) -> bool:
        return self.supabase.prompt_output_exists(prompt_id, brand_id, batch_id, llm_model_filter=llm_model_filter)

    def find_existing_prompt_output(
        self,
        prompt_id: str,
        brand_id: str,
        batch_id: str | None,
        *,
        llm_model_filter: str | None = "gpt",
        required_models: list[str] | None = None,
    ) -> dict[str, Any] | None:
        return self.supabase.find_existing_prompt_output(
            prompt_id, brand_id, batch_id,
            llm_model_filter=llm_model_filter,
            required_models=required_models,
        )

    def save_prompt_output(self, output: dict[str, Any], max_retries: int = 4) -> dict[str, Any] | None:
        for attempt in range(max_retries + 1):
            try:
                return self.supabase.save_prompt_output(output)
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(min(60, 2**attempt))
        return None

    def save_prompt_output_products(self, products: list[dict[str, Any]], max_retries: int = 4) -> list[dict[str, Any]]:
        for attempt in range(max_retries + 1):
            try:
                return self.supabase.save_prompt_output_products(products)
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(min(60, 2**attempt))
        return []

    def save_prompt_output_suggestions(
        self, suggestions: list[dict[str, Any]], max_retries: int = 4
    ) -> list[dict[str, Any]]:
        for attempt in range(max_retries + 1):
            try:
                return self.supabase.save_prompt_output_suggestions(suggestions)
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(min(60, 2**attempt))
        return []

    def save_prompt_output_entities(self, entities: list[dict[str, Any]], max_retries: int = 4) -> list[dict[str, Any]]:
        for attempt in range(max_retries + 1):
            try:
                return self.supabase.save_prompt_output_entities(entities)
            except Exception:
                if attempt >= max_retries:
                    raise
                time.sleep(min(60, 2**attempt))
        return []

    def get_prompt_output(self, output_id: int | str) -> dict[str, Any] | None:
        return self.supabase.get_prompt_output(output_id)

    def get_prompt_outputs(
        self,
        *,
        output_id: int | str | None = None,
        batch_id: str | None = None,
        brand_id: str | None = None,
        prompt_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.supabase.get_prompt_outputs(
            output_id=output_id,
            batch_id=batch_id,
            brand_id=brand_id,
            prompt_id=prompt_id,
            limit=limit,
        )

    def update_prompt_output(self, output: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any] | None:
        return self.supabase.update_prompt_output(output, patch)

    # ── Prompt claiming ────────────────────────────────────────────────────────

    def try_claim_prompt(
        self,
        prompt_id: str,
        batch_id: str,
        brand_id: str,
        llm_model: str,
        worker_id: str,
        ttl_minutes: int = 5,
    ) -> bool:
        """Atomically claim a prompt. Returns True if this worker holds the claim."""
        return self.supabase.try_claim_prompt(prompt_id, batch_id, brand_id, llm_model, worker_id, ttl_minutes)

    def release_claim(
        self,
        prompt_id: str,
        batch_id: str,
        llm_model: str,
        error_message: str | None = None,
    ) -> None:
        """Mark a claim failed so the prompt is available for retry."""
        self.supabase.release_claim(prompt_id, batch_id, llm_model, error_message=error_message)

    def complete_claim(self, prompt_id: str, batch_id: str, llm_model: str) -> None:
        """Delete a claim after successful processing."""
        self.supabase.complete_claim(prompt_id, batch_id, llm_model)

    @property
    def supabase(self) -> SupabasePromptOutputRepository:
        if not self.supabase_url:
            raise RuntimeError("Missing Supabase URL. Set BRANDSIGHT_SUPABASE_URL or BRANDSIGHT_API_BASE_URL.")
        if not hasattr(self, "_supabase"):
            object.__setattr__(
                self,
                "_supabase",
                SupabasePromptOutputRepository(
                    supabase_url=self.supabase_url,
                    anon_key=self.anon_key,
                    table_name=self.prompt_outputs_table,
                    product_table_name=self.prompt_output_products_table,
                    entity_table_name=self.prompt_output_entities_table,
                    suggestion_table_name=self.prompt_output_suggestions_table,
                ),
            )
        return getattr(self, "_supabase")
