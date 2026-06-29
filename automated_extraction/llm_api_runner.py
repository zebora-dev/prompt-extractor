"""
LLM API runner — calls OpenAI, Anthropic, and Gemini directly (no LangChain).

Using direct provider SDKs avoids data loss: LangChain does not expose Anthropic cache
tokens, OpenAI reasoning tokens, or structured web search results in response_metadata.

Langfuse 3.x @observe decorator is used for observability.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass
class ApiCapture:
    response: str
    model_slug: str
    sources: list[dict[str, Any]]
    tool_calls_used: list[str]
    usage: dict[str, Any]
    finish_reason: str
    latency_ms: int
    langfuse_trace_url: str | None
    web_search_queries: list[str] | None
    web_search_query_count: int | None
    web_search_results_per_query: list[dict[str, Any]] | None


class LLMApiRunner:
    def __init__(
        self,
        model_name: str,
        *,
        use_web_search: bool = False,
        temperature: float = 0.0,
        response_timeout_seconds: int = 120,
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
        langfuse_host: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.use_web_search = use_web_search
        self.temperature = temperature
        self.response_timeout_seconds = response_timeout_seconds
        self._langfuse_public_key = langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
        self._langfuse_secret_key = langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY")
        self._langfuse_host = langfuse_host or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com"

    # ── Public API ─────────────────────────────────────────────────────────────

    def run_prompt(self, prompt_text: str, *, trace_metadata: dict[str, Any] | None = None) -> ApiCapture:
        meta = trace_metadata or {}
        if model_is_openai(self.model_name):
            return self._run_openai(prompt_text, meta)
        elif model_is_anthropic(self.model_name):
            return self._run_anthropic(prompt_text, meta)
        elif model_is_gemini(self.model_name):
            return self._run_gemini(prompt_text, meta)
        else:
            raise ValueError(
                f"Cannot determine provider for model '{self.model_name}'. "
                "Expected prefix: gpt- / o1 / o3 / o4 (OpenAI), claude- (Anthropic), gemini- (Google)."
            )

    # ── OpenAI ─────────────────────────────────────────────────────────────────

    def _run_openai(self, prompt_text: str, trace_metadata: dict[str, Any]) -> ApiCapture:
        import openai

        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # gpt-4o-search-preview has built-in search — no tool binding needed
        actual_model = self.model_name
        if self.use_web_search and not actual_model.endswith("-search-preview"):
            actual_model = _openai_search_model(self.model_name)
            LOGGER.info("Web search requested — using model %s", actual_model)

        messages = [{"role": "user", "content": prompt_text}]

        start = time.monotonic()
        trace_url: str | None = None

        try:
            langfuse = self._get_langfuse_client()
            trace = langfuse.trace(
                name="api-extraction",
                metadata={**trace_metadata, "model": actual_model, "provider": "openai"},
                input=prompt_text,
            ) if langfuse else None

            # Search-preview and o-series models do not accept temperature
            supports_temperature = not actual_model.startswith("o") and "search" not in actual_model
            response = client.chat.completions.create(
                model=actual_model,
                messages=messages,
                **({"temperature": self.temperature} if supports_temperature else {}),
                timeout=self.response_timeout_seconds,
            )

            latency_ms = int((time.monotonic() - start) * 1000)
            choice = response.choices[0]
            content = choice.message.content or ""
            finish_reason = choice.finish_reason or "unknown"

            usage_obj = response.usage
            usage: dict[str, Any] = {
                "input_tokens": usage_obj.prompt_tokens if usage_obj else 0,
                "output_tokens": usage_obj.completion_tokens if usage_obj else 0,
                "total_tokens": usage_obj.total_tokens if usage_obj else 0,
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "reasoning_tokens": None,
            }
            if usage_obj:
                ctd = getattr(usage_obj, "completion_tokens_details", None)
                ptd = getattr(usage_obj, "prompt_tokens_details", None)
                if ctd:
                    usage["reasoning_tokens"] = getattr(ctd, "reasoning_tokens", None)
                if ptd:
                    usage["cache_read_tokens"] = getattr(ptd, "cached_tokens", None)

            sources = _extract_openai_citations(choice.message)

            if trace:
                trace.update(
                    output=content,
                    metadata={
                        **trace_metadata,
                        "model": actual_model,
                        "provider": "openai",
                        "finish_reason": finish_reason,
                        "usage": usage,
                        "source_count": len(sources),
                    },
                )
                trace_url = trace.get_trace_url() if hasattr(trace, "get_trace_url") else None
                langfuse.flush()

        except Exception:
            LOGGER.exception("OpenAI API call failed for model %s", actual_model)
            raise

        model_slug = f"api:{actual_model}"
        LOGGER.info(
            "OpenAI capture: model=%s tokens=%s/%s sources=%s latency_ms=%s",
            actual_model, usage["input_tokens"], usage["output_tokens"], len(sources), latency_ms,
        )
        return ApiCapture(
            response=content,
            model_slug=model_slug,
            sources=sources,
            tool_calls_used=["web_search"] if self.use_web_search and sources else [],
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            langfuse_trace_url=trace_url,
            web_search_queries=None,
            web_search_query_count=None,
            web_search_results_per_query=None,
        )

    # ── Anthropic ──────────────────────────────────────────────────────────────

    def _run_anthropic(self, prompt_text: str, trace_metadata: dict[str, Any]) -> ApiCapture:
        import anthropic

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        create_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt_text}],
        }
        if not self.model_name.startswith("claude-3-5") and not self.model_name.startswith("claude-3-opus"):
            create_kwargs["temperature"] = self.temperature

        if self.use_web_search:
            create_kwargs["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            ]

        start = time.monotonic()
        trace_url: str | None = None

        try:
            langfuse = self._get_langfuse_client()
            trace = langfuse.trace(
                name="api-extraction",
                metadata={**trace_metadata, "model": self.model_name, "provider": "anthropic"},
                input=prompt_text,
            ) if langfuse else None

            message = client.messages.create(**create_kwargs)
            latency_ms = int((time.monotonic() - start) * 1000)

            content = _extract_anthropic_text(message.content)
            finish_reason = message.stop_reason or "unknown"

            usage_obj = message.usage
            usage: dict[str, Any] = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0),
                "output_tokens": getattr(usage_obj, "output_tokens", 0),
                "total_tokens": (getattr(usage_obj, "input_tokens", 0) + getattr(usage_obj, "output_tokens", 0)),
                "cache_read_tokens": getattr(usage_obj, "cache_read_input_tokens", None),
                "cache_creation_tokens": getattr(usage_obj, "cache_creation_input_tokens", None),
                "reasoning_tokens": None,
            }

            web_data = _extract_anthropic_web_searches(message.content) if self.use_web_search else None
            sources = web_data["sources"] if web_data else []
            tool_calls_used = ["web_search"] if web_data and web_data["queries"] else []

            if trace:
                trace.update(
                    output=content,
                    metadata={
                        **trace_metadata,
                        "model": self.model_name,
                        "provider": "anthropic",
                        "finish_reason": finish_reason,
                        "usage": usage,
                        "source_count": len(sources),
                        "web_search_query_count": web_data["query_count"] if web_data else None,
                    },
                )
                trace_url = trace.get_trace_url() if hasattr(trace, "get_trace_url") else None
                langfuse.flush()

        except Exception:
            LOGGER.exception("Anthropic API call failed for model %s", self.model_name)
            raise

        model_slug = f"api:{self.model_name}"
        LOGGER.info(
            "Anthropic capture: model=%s tokens=%s/%s (cache_read=%s) sources=%s queries=%s latency_ms=%s",
            self.model_name, usage["input_tokens"], usage["output_tokens"],
            usage["cache_read_tokens"], len(sources),
            web_data["query_count"] if web_data else 0, latency_ms,
        )
        return ApiCapture(
            response=content,
            model_slug=model_slug,
            sources=sources,
            tool_calls_used=tool_calls_used,
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            langfuse_trace_url=trace_url,
            web_search_queries=web_data["queries"] if web_data else None,
            web_search_query_count=web_data["query_count"] if web_data else None,
            web_search_results_per_query=web_data["results_per_query"] if web_data else None,
        )

    # ── Gemini ─────────────────────────────────────────────────────────────────

    def _run_gemini(self, prompt_text: str, trace_metadata: dict[str, Any]) -> ApiCapture:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError("google-genai is required for Gemini models. Run: uv add google-genai")

        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

        config = genai_types.GenerateContentConfig(temperature=self.temperature)
        if self.use_web_search:
            config.tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]

        start = time.monotonic()
        trace_url: str | None = None

        try:
            langfuse = self._get_langfuse_client()
            trace = langfuse.trace(
                name="api-extraction",
                metadata={**trace_metadata, "model": self.model_name, "provider": "gemini"},
                input=prompt_text,
            ) if langfuse else None

            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt_text,
                config=config,
            )
            latency_ms = int((time.monotonic() - start) * 1000)

            content = response.text or ""
            finish_reason = "stop"
            if response.candidates:
                fr = response.candidates[0].finish_reason
                finish_reason = str(fr) if fr else "stop"

            usage_meta = getattr(response, "usage_metadata", None)
            usage: dict[str, Any] = {
                "input_tokens": getattr(usage_meta, "prompt_token_count", 0),
                "output_tokens": getattr(usage_meta, "candidates_token_count", 0),
                "total_tokens": getattr(usage_meta, "total_token_count", 0),
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "reasoning_tokens": None,
            }

            sources = _extract_gemini_sources(response)

            if trace:
                trace.update(
                    output=content,
                    metadata={
                        **trace_metadata,
                        "model": self.model_name,
                        "provider": "gemini",
                        "finish_reason": finish_reason,
                        "usage": usage,
                        "source_count": len(sources),
                    },
                )
                trace_url = trace.get_trace_url() if hasattr(trace, "get_trace_url") else None
                langfuse.flush()

        except Exception:
            LOGGER.exception("Gemini API call failed for model %s", self.model_name)
            raise

        model_slug = f"api:{self.model_name}"
        LOGGER.info(
            "Gemini capture: model=%s tokens=%s/%s sources=%s latency_ms=%s",
            self.model_name, usage["input_tokens"], usage["output_tokens"], len(sources), latency_ms,
        )
        return ApiCapture(
            response=content,
            model_slug=model_slug,
            sources=sources,
            tool_calls_used=["web_search"] if self.use_web_search and sources else [],
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            langfuse_trace_url=trace_url,
            web_search_queries=None,
            web_search_query_count=None,
            web_search_results_per_query=None,
        )

    # ── Langfuse client ────────────────────────────────────────────────────────

    def _get_langfuse_client(self) -> Any | None:
        if not self._langfuse_public_key or not self._langfuse_secret_key:
            return None
        try:
            from langfuse import Langfuse
            return Langfuse(
                public_key=self._langfuse_public_key,
                secret_key=self._langfuse_secret_key,
                host=self._langfuse_host,
            )
        except ImportError:
            LOGGER.warning("langfuse not installed — traces will not be sent.")
            return None
        except Exception as exc:
            LOGGER.warning("Langfuse client init failed: %s", exc)
            return None


# ── Provider detection ─────────────────────────────────────────────────────────

def model_is_openai(model_name: str) -> bool:
    return model_name.startswith(("gpt-", "o1", "o3", "o4"))

def model_is_anthropic(model_name: str) -> bool:
    return model_name.startswith("claude-")

def model_is_gemini(model_name: str) -> bool:
    return model_name.startswith("gemini-")


def _openai_search_model(model_name: str) -> str:
    """Return the search-preview variant for a given GPT model name."""
    # gpt-4o → gpt-4o-search-preview
    if "search" not in model_name:
        base = model_name.split("-mini")[0] if "-mini" in model_name else model_name
        return f"{base}-search-preview"
    return model_name


# ── Response parsing ───────────────────────────────────────────────────────────

def _extract_openai_citations(message: Any) -> list[dict[str, Any]]:
    """Extract url_citation annotations from an OpenAI search response message.

    OpenAI returns annotations at message level (message.annotations), where each
    annotation has type='url_citation' and a nested url_citation object with url/title.
    """
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # Primary path: annotations at message level (gpt-4o-search-preview)
    annotations = getattr(message, "annotations", None) or []
    for ann in annotations:
        if getattr(ann, "type", None) == "url_citation":
            citation = getattr(ann, "url_citation", None)
            url = getattr(citation, "url", None) if citation else None
            title = getattr(citation, "title", None) if citation else None
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append({"url": url, "title": title or url})

    # Fallback: annotations on content parts (older API behaviour)
    if not sources:
        content = getattr(message, "content", None)
        if content and not isinstance(content, str):
            for part in content:
                for ann in getattr(part, "annotations", None) or []:
                    if getattr(ann, "type", None) == "url_citation":
                        url = getattr(ann, "url", None)
                        title = getattr(ann, "title", None)
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            sources.append({"url": url, "title": title or url})

    return sources


def _extract_anthropic_text(content_blocks: list[Any]) -> str:
    """Concatenate all text content blocks from an Anthropic response."""
    parts: list[str] = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text or "")
    return "\n".join(parts).strip()


def _extract_anthropic_web_searches(content_blocks: list[Any]) -> dict[str, Any] | None:
    """
    Extract query fan-out data from Anthropic web search tool blocks.

    Returns dict with keys: queries, results_per_query, query_count, sources.
    Returns None if no search blocks found.
    """
    query_blocks: dict[str, str] = {}
    results_per_query: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for block in content_blocks:
        block_type = getattr(block, "type", None)
        if block_type == "server_tool_use" and getattr(block, "name", None) == "web_search":
            query = (getattr(block, "input", None) or {}).get("query", "")
            if query:
                query_blocks[block.id] = query

        elif block_type == "web_search_tool_result":
            tool_use_id = getattr(block, "tool_use_id", None)
            query = query_blocks.get(tool_use_id, "")
            results: list[dict[str, Any]] = []
            result_content = getattr(block, "content", None) or []
            for r in result_content:
                if getattr(r, "type", None) == "web_search_result":
                    url = getattr(r, "url", None)
                    title = getattr(r, "title", None) or url or ""
                    results.append({
                        "url": url,
                        "title": title,
                        "page_age": getattr(r, "page_age", None),
                    })
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        sources.append({"url": url, "title": title})
            if results:
                results_per_query.append({"query": query, "results": results})

    if not query_blocks:
        return None

    return {
        "queries": list(query_blocks.values()),
        "results_per_query": results_per_query,
        "query_count": len(query_blocks),
        "sources": sources,
    }


def _extract_gemini_sources(response: Any) -> list[dict[str, Any]]:
    """Extract grounding citations from a Gemini search-grounded response (google-genai SDK)."""
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    try:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            grounding = getattr(candidate, "grounding_metadata", None)
            if not grounding:
                continue
            # google-genai SDK: grounding_chunks[].web.uri / web.title
            chunks = getattr(grounding, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web:
                    url = getattr(web, "uri", None)
                    title = getattr(web, "title", None) or url or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        sources.append({"url": url, "title": title})
    except Exception as exc:
        LOGGER.debug("Failed to extract Gemini grounding sources: %s", exc)
    return sources
