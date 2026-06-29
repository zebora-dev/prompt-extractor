# LLM API Extraction — Architecture & Operations Guide

## Overview

The API extraction pipeline sends prompts directly to LLM provider REST APIs (OpenAI, Anthropic,
Google) — no browser, no Chrome profile, no VNC. It shares the same Prefect / Fly.io / Supabase
infrastructure as the browser-based scrapers and outputs records into the same `prompt_outputs`
table, distinguished by `llm_model` values prefixed with `api:` (e.g. `api:gpt-4o`,
`api:claude-opus-4-8`, `api:gemini-2.0-flash`).

The `api:` prefix ensures each model gets its own completion slot — running `gpt-4o` and
`claude-opus-4-8` against the same batch will track remaining counts independently.

---

## How it works

### 1. Runner (`automated_extraction/llm_api_runner.py`)

`LLMApiRunner` dispatches to the appropriate provider SDK based on the model name prefix:

| Prefix | Provider | SDK |
|---|---|---|
| `gpt-`, `o1`, `o3`, `o4` | OpenAI | `openai` |
| `claude-` | Anthropic | `anthropic` |
| `gemini-` | Google | `google-generativeai` |

Key steps per prompt:

1. **Detect provider** from `model_name` prefix.
2. **Call the API** using the provider's native Python SDK — no LangChain.
3. **Capture response text** from the completion.
4. **Extract sources** — citations from web search results (when `use_web_search=True`).
5. **Capture token usage** — input, output, cache read/creation tokens, reasoning tokens.
6. **Record latency** in milliseconds.
7. **Emit a Langfuse trace** (if `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set).
8. Return an `ApiCapture` dataclass with all fields.

### 2. Provider-specific web search

Web search is opt-in via `--use-web-search`. Each provider uses its own native mechanism:

**OpenAI** — swaps the model to `gpt-4o-search-preview`. Only final `url_citation` annotations
are returned; individual search queries are not exposed by the API.

**Anthropic** — passes `tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]`.
Full query fan-out is captured: `server_tool_use` blocks expose each search query string, and
`web_search_tool_result` blocks expose results per query (URL, title, page_age). This gives
complete visibility into how the model decomposed the prompt into searches.

**Gemini** — passes `tools=["google_search_retrieval"]`. Grounding metadata chunks (URL, title)
are extracted; individual queries are not exposed.

### 3. Extraction pipeline (`automated_extraction/extraction.py`)

`run_api_extraction_job()` mirrors the Claude and Perplexity extraction jobs:

1. `load_prompt_work()` fetches remaining prompts filtered by `llm_model_filter=f"api:{model_name}"`.
2. `try_claim_prompt()` acquires a distributed lock (prevents duplicate work across concurrent workers).
3. `LLMApiRunner.run_prompt()` calls the provider API and returns an `ApiCapture`.
4. `build_api_prompt_output()` constructs the DB payload.
5. `ApiClient.save_prompt_output()` writes to `prompt_outputs`.
6. `complete_claim()` / `release_claim()` closes the lock.

### 4. Data model

Each record in `prompt_outputs` contains:

| Field | Value |
|---|---|
| `llm_model` | `api:gpt-4o`, `api:claude-opus-4-8`, `api:gemini-2.0-flash`, etc. |
| `config.site` | `"OpenAI API"` / `"Anthropic API"` / `"Google API"` |
| `output_metadata.site_used` | Same as `config.site` |
| `sources` | `[{url, title}]` — citations from web search (empty if `use_web_search=False`) |
| `output_metadata.original_metadata` | See below |

`original_metadata` fields:

```json
{
  "model_name": "gpt-4o",
  "use_web_search": false,
  "tool_calls_used": [],
  "token_usage": {
    "input_tokens": 312,
    "output_tokens": 487,
    "total_tokens": 799,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "reasoning_tokens": 0
  },
  "finish_reason": "stop",
  "latency_ms": 3241,
  "langfuse_trace_url": "https://cloud.langfuse.com/trace/...",
  "web_search_queries": null,
  "web_search_query_count": null,
  "web_search_results_per_query": null
}
```

When `use_web_search=True` with Anthropic, `web_search_queries` and `web_search_results_per_query`
are populated with full fan-out detail:

```json
{
  "web_search_queries": ["Nationwide savings accounts UK 2024", "Nationwide ISA rates"],
  "web_search_query_count": 2,
  "web_search_results_per_query": [
    {
      "query": "Nationwide savings accounts UK 2024",
      "results": [
        {"url": "https://...", "title": "...", "page_age": "2024-11-01"}
      ]
    }
  ]
}
```

### 5. Prefect flows & tasks

| Name | File | Purpose |
|---|---|---|
| `api-extraction` | `workflows/flows.py` | Single run: one set of prompts |
| `api-extraction-batch` | `workflows/flows.py` | Loops `api-extraction` until remaining = 0 |
| `extract_api_batch_task` | `workflows/tasks.py` | Prefect task wrapping `run_api_extraction_job` |

### 6. Fly.io deployment

| App | Region | Work pool | Config | VM size |
|---|---|---|---|---|
| `prompt-extractor-api-uk` | lhr (London) | `prompt-extraction-api-uk` | `fly-api-uk.yaml` | performance-1x, **2 GB RAM** |

No volume needed — no Chrome profile, no persistent state. `--limit 3` allows up to 3 concurrent
Prefect flow runs per machine (API calls are lightweight).

---

## Running an extraction

### Local test

```bash
uv run python scripts/test_api_local.py \
  --batch-id <BATCH_ID> \
  --model gpt-4o \
  --measurements-filter Visibility \
  --limit 3
```

### Via CLI

```bash
python -m automated_extraction \
  --provider api \
  --batch-id <BATCH_ID> \
  --model gpt-4o \
  --limit 10
```

### Via dispatch loop (recommended for production)

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
python scripts/dispatch_api_loop.py \
  --batch-id <BATCH_ID> \
  --model gpt-4o \
  --measurements-filter Visibility \
  --region uk \
  --worker-count 1 \
  --limit 10 \
  --poll-interval 120
```

The loop polls every 2 minutes (configurable), re-dispatches if prompts remain but no flows are
running, and exits when remaining count reaches 0.

### With web search enabled

```bash
python scripts/dispatch_api_loop.py \
  --batch-id <BATCH_ID> \
  --model gpt-4o \
  --use-web-search \
  --region uk \
  --limit 10
```

For Anthropic web search (full query fan-out):

```bash
python scripts/dispatch_api_loop.py \
  --batch-id <BATCH_ID> \
  --model claude-opus-4-8 \
  --use-web-search \
  --region uk \
  --limit 5
```

---

## Fly.io deployment

### Initial deploy

```bash
flyctl apps create prompt-extractor-api-uk
flyctl deploy -a prompt-extractor-api-uk -c fly-api-uk.yaml
```

### Required secrets

```bash
flyctl secrets set -a prompt-extractor-api-uk \
  BRANDSIGHT_API_BASE_URL=... \
  BRANDSIGHT_SUPABASE_URL=... \
  BRANDSIGHT_SUPABASE_ANON_KEY=... \
  BRANDSIGHT_SUPABASE_SERVICE_KEY=... \
  BRANDSIGHT_PROMPT_OUTPUTS_TABLE=... \
  BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE=... \
  BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE=... \
  BRANDSIGHT_SCORE_WORKFLOW_URL=... \
  BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN=false \
  PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
  SLACK_BOT_TOKEN=... \
  FLY_API_TOKEN=$(flyctl auth token) \
  OPENAI_API_KEY=... \
  ANTHROPIC_API_KEY=... \
  GOOGLE_API_KEY=...
```

Langfuse secrets (optional — traces are silently skipped if unset):

```bash
flyctl secrets set -a prompt-extractor-api-uk \
  LANGFUSE_PUBLIC_KEY=... \
  LANGFUSE_SECRET_KEY=... \
  LANGFUSE_HOST=https://cloud.langfuse.com
```

### Registering Prefect deployments

After deploying or changing flow code:

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-api-uk \
PREFECT_WORKING_DIR=/app \
  python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

This registers `api-extraction-uk` and `api-extraction-batch-uk` against the
`prompt-extraction-api-uk` work pool.

---

## Starting and stopping machines

The API app machines can be stopped between batches to avoid idle costs (no persistent Chrome
session to maintain — machines start cold cleanly):

```bash
# Stop
flyctl machine stop <MACHINE_ID> -a prompt-extractor-api-uk

# Start
flyctl machine start <MACHINE_ID> -a prompt-extractor-api-uk

# List machines
flyctl machine list -a prompt-extractor-api-uk
```

The dispatch loop does **not** currently auto-stop the API machines on completion (unlike the
browser scrapers). Stop them manually once a batch is done, or add `FLY_API_TOKEN` to the local
`.env` to enable the `scale_down_fly()` path in `dispatch_api_loop.py`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for `gpt-*`, `o1`, `o3`, `o4` models |
| `ANTHROPIC_API_KEY` | — | Required for `claude-*` models |
| `GOOGLE_API_KEY` | — | Required for `gemini-*` models |
| `LLM_API_DEFAULT_MODEL` | `gpt-4o` | Default model for `--provider api` |
| `LLM_API_TEMPERATURE` | `0.0` | Sampling temperature |
| `LLM_API_RESPONSE_TIMEOUT_SECONDS` | `120` | Per-request timeout |
| `LANGFUSE_PUBLIC_KEY` | — | Optional Langfuse observability |
| `LANGFUSE_SECRET_KEY` | — | Optional Langfuse observability |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse instance URL |

---

## Key differences from browser scrapers

| Feature | Browser scrapers (ChatGPT / Claude / Perplexity) | API extraction |
|---|---|---|
| Infrastructure | Chrome + VNC + Selenium + 4–8 GB RAM | Direct HTTP + 2 GB RAM |
| Chrome profile | Required (pre-authenticated session) | Not needed |
| Concurrency | 1 prompt per machine (browser session) | 3 concurrent flows per machine |
| `llm_model` prefix | `gpt-4o`, `claude-sonnet-*`, `perplexity-sonar` | `api:gpt-4o`, `api:claude-opus-4-8` |
| Web search | N/A (uses whatever the UI provides) | Native tool per provider |
| Query fan-out | Not captured | Captured for Anthropic; not exposed by OpenAI/Google |
| Token usage | Not captured | Full input/output/cache/reasoning tokens |
| Latency tracking | Not captured | `latency_ms` in `original_metadata` |
| Observability | Prefect logs only | Prefect logs + optional Langfuse traces |

---

## Why no LangChain

LangChain was evaluated and rejected. These fields are silently dropped by `response.response_metadata`:

- **Anthropic**: `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens`
- **OpenAI**: `usage.completion_tokens_details.reasoning_tokens`, `system_fingerprint`
- **Both**: structured web search tool results (citations, query strings, per-query result sets)

A thin direct-SDK factory (~50 lines in `llm_api_runner.py`) captures everything with zero data
loss and no extra dependency.

---

## Debugging

### Check remaining prompts for a model

```bash
uv run python -c "
from automated_extraction.config import Settings
from automated_extraction.api_client import ApiClient
settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)
api = ApiClient(settings.api_base_url, settings.anon_key, supabase_url=settings.supabase_url,
    prompt_outputs_table=settings.prompt_outputs_table,
    prompt_output_products_table=settings.prompt_output_products_table,
    prompt_output_entities_table=settings.prompt_output_entities_table)
remaining = api.get_prompts('<BATCH_ID>', '<BRAND_ID>', only_remaining=True,
    llm_model_filter='api:gpt-4o', measurements_filter='Visibility')
print(f'Remaining: {len(remaining)}')
"
```

### View saved records

```bash
uv run python -c "
from automated_extraction.config import Settings
from supabase import create_client
settings = Settings.from_env(require_api_key=True, require_auto_login_credentials=False)
client = create_client(settings.supabase_url, settings.anon_key)
result = client.table('prompts_outputs').select('id,llm_model,run_at').eq('batch_id', '<BATCH_ID>').ilike('llm_model', 'api:%').order('id', desc=True).limit(10).execute()
for r in result.data: print(r)
"
```

### Check Fly worker logs

```bash
flyctl logs -a prompt-extractor-api-uk
```

### Test a single prompt locally

```bash
uv run python scripts/test_api_local.py \
  --batch-id <BATCH_ID> \
  --model gpt-4o \
  --limit 1 \
  --force-rerun
```
