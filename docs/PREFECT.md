# Prefect Operations Guide

This app runs extraction workflows as Prefect flows. Prefect provides
observable runs, parameters, logs, duration, state, retry hooks, and a UI for
triggering and inspecting work.

Batch, prompt, and prompt-output operations go directly through the Python
Supabase client.

> **For the ChatGPT worker fleet (Fly.io machines, persistent Chrome, VNC
> login)** see [GPT_WORKERS.md](./GPT_WORKERS.md).

---

## Flow Reference

### ChatGPT extraction

| Flow name | Deployment (UK) | Purpose |
|-----------|----------------|---------|
| `chatgpt-extraction` | `chatgpt-extraction-uk` | Single run: load prompts → ChatGPT → save outputs → trigger scoring |
| `chatgpt-extraction-batch` | `chatgpt-extraction-batch-uk` | Loop `chatgpt-extraction` in chunks until a batch is fully covered |
| `dispatch-extraction` | `dispatch-extraction-uk` | Count remaining prompts, split across N workers, submit one batch run per worker |

### Google extraction

| Flow name | Deployment (UK) | Purpose |
|-----------|----------------|---------|
| `google-ai-mode-extraction` | `google-ai-mode-extraction-uk` | Single Google AI Mode run |
| `google-ai-mode-extraction-batch` | `google-ai-mode-extraction-batch-uk` | Loop Google AI Mode in chunks |
| `google-ai-overview-extraction` | `google-ai-overview-extraction-uk` | Single Google AI Overview run |
| `google-ai-overview-extraction-batch` | `google-ai-overview-extraction-batch-uk` | Loop Google AI Overview in chunks |

### Post-processing

| Flow name | Deployment (UK) | Purpose |
|-----------|----------------|---------|
| `prompt-output-processing` | `prompt-output-processing-uk` | Re-process saved outputs (HTML→markdown enrichment) without re-running extraction |

---

## End-to-End Pipeline

Each `chatgpt-extraction` run executes these tasks in order:

```
extract-chatgpt-batch
  └─ For each prompt:
       ├─ Claim prompt (atomic — prevents duplicate work across workers)
       ├─ Connect to persistent Chrome (localhost:9222)
       ├─ Create fresh ChatGPT chat
       ├─ Send prompt, wait for response
       ├─ Copy markdown + raw HTML + sources
       ├─ Capture product flyouts  (if capture_products=true)
       └─ Capture entity flyouts   (if capture_entities=true)

product-output-process
  └─ Save captured products to prompts_outputs_products

entity-output-process
  └─ Save captured entities to prompts_outputs_entities

prompt-output-process
  └─ Convert raw HTML → markdown, enrich response with images/links

trigger-score-workflow
  └─ POST each output_id to BRANDSIGHT_SCORE_WORKFLOW_URL
```

---

## Remote Prefect Server

The production Prefect server runs on Fly.io:

```
https://prompt-extractor-prefect.fly.dev
```

All CLI commands against production must set `PREFECT_API_URL`:

```bash
export PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api
```

Or prefix each command:

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api prefect deployment ls
```

---

## 1. Install Dependencies

```bash
cd automated-extraction
source .venv/bin/activate
make install
```

---

## 2. Configure Environment

```bash
cp .env.example .env
```

Required variables:

```text
BRANDSIGHT_SUPABASE_ANON_KEY=...
BRANDSIGHT_SUPABASE_URL=https://hmwgplzdzffivawkflci.supabase.co
BRANDSIGHT_PROMPT_OUTPUTS_TABLE=prompts_outputs
BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE=prompts_outputs_products
BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE=prompts_outputs_entities
BRANDSIGHT_SCORE_WORKFLOW_URL=https://workflow.zebora.io/api/workflows/score-single-output
WORKFLOW_API_KEY=...
```

For local development:

```text
PREFECT_API_URL=http://localhost:4200/api
PREFECT_WORK_POOL=prompt-extraction-pool
```

For remote (production):

```text
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api
PREFECT_WORK_POOL=prompt-extraction-uk
```

---

## 3. Local Development

### Start a local Prefect server

```bash
make prefect-server
# UI: http://localhost:4200
```

### Serve flows locally (simplest dev mode)

```bash
make prefect-serve
```

Registers and serves all deployments in the same Python process.

### Deploy to a local process work pool

```bash
make prefect-pool    # create the work pool
make prefect-deploy  # register deployments
make prefect-worker  # start a worker
```

---

## 4. Triggering Flows (Remote)

### Run a single ChatGPT extraction (one worker, small test)

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'chatgpt-extraction/chatgpt-extraction-uk' \
  --param batch_id=<batch-uuid> \
  --param limit=2 \
  --param login_email=dev@theround.com \
  --param capture_products=true \
  --param capture_entities=true
```

### Run the batch flow on a specific worker

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'chatgpt-extraction-batch/chatgpt-extraction-batch-uk' \
  --param batch_id=<batch-uuid> \
  --param limit=5 \
  --param login_email=dev@theround.com \
  --param capture_products=true \
  --param capture_entities=true
```

`login_email` pins the run to the machine holding that account. Omit it to let
Prefect assign any available worker.

### Dispatch across all workers

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<batch-uuid> \
  --param worker_count=9 \
  --param capture_products=true \
  --param capture_entities=true
```

The dispatcher counts remaining prompts, divides them into `worker_count`
chunks, and submits one `chatgpt-extraction-batch-uk` run per chunk with a
staggered startup so workers don't all hit ChatGPT simultaneously.

### Re-process saved outputs without re-running ChatGPT

```bash
# By output ID
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'prompt-output-processing/prompt-output-processing-uk' \
  --param output_id=12326

# By batch (latest N outputs)
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'prompt-output-processing/prompt-output-processing-uk' \
  --param batch_id=<batch-uuid> \
  --param limit=10
```

### Force re-run prompts that already have outputs

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'chatgpt-extraction/chatgpt-extraction-uk' \
  --param batch_id=<batch-uuid> \
  --param limit=1 \
  --param force_rerun=true
```

---

## 5. Registering Deployments

After code changes, re-register deployments against the remote server:

```bash
source .venv/bin/activate
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

This must be run from the directory containing the updated code.

---

## 6. Parameters Reference

### `chatgpt-extraction` / `chatgpt-extraction-uk`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_id` | string | — | BrandSight batch UUID (required unless `prompts_file` set) |
| `prompts_file` | string/null | null | Local JSON prompt file (alternative to `batch_id`) |
| `brand_id` | string/null | null | Brand UUID override (for local files) |
| `limit` | integer/null | null | Max prompts to process |
| `skip` | integer | 0 | Number of prompts to skip |
| `dry_run` | boolean | false | Load prompts and preview without running ChatGPT |
| `headless` | boolean/null | null | Override `CHATGPT_HEADLESS`; null uses env |
| `chrome_user_data_dir` | string/null | null | Override Chrome profile path |
| `force_rerun` | boolean | false | Run even if output already exists |
| `llm_model_filter` | string | `gpt` | Filter: only treat outputs with this model as complete |
| `auto_login` | boolean | false | Use automated login (requires `CHATGPT_ACCOUNTS_B64`) |
| `login_email` | string/null | null | Account to use; pins run to that machine |
| `capture_products` | boolean | false | Capture product flyouts after each response |
| `capture_entities` | boolean | false | Capture entity flyouts after each response |
| `sources_panel_pause_seconds` | integer | 0 | Debug pause after opening Sources panel |

### `chatgpt-extraction-batch` / `chatgpt-extraction-batch-uk`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_id` | string | — | BrandSight batch UUID |
| `model_filter` | string | `gpt` | LLM model filter passed to each sub-run |
| `limit` | integer | 5 | Prompts per sub-run |
| `skip` | integer | 0 | Skip N prompts from the start |
| `login_email` | string/null | null | Pins all sub-runs to a specific machine |
| `capture_products` | boolean | false | Enable product capture |
| `capture_entities` | boolean | false | Enable entity capture |
| `delay_seconds` | integer | 120 | Wait between sub-runs (rate limiting) |
| `startup_delay_seconds` | integer | 0 | Stagger delay set by dispatcher |
| `auto_login` | boolean | false | Use automated login |

### `dispatch-extraction` / `dispatch-extraction-uk`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `batch_id` | string | — | BrandSight batch UUID |
| `extraction_type` | string | `chatgpt` | `chatgpt`, `google-ai-mode`, or `google-ai-overview` |
| `worker_count` | integer | 1 | Number of parallel workers to dispatch |
| `limit` | integer/null | null | Cap total prompts dispatched (across all workers) |
| `capture_products` | boolean | false | Enable product capture |
| `capture_entities` | boolean | false | Enable entity capture |
| `delay_seconds` | integer | 120 | Delay between sub-runs within each worker |
| `stagger_seconds` | integer | 30 | Startup stagger between workers |

---

## 7. Batch Loop Behaviour

The `*-batch` flows loop sub-runs until all remaining prompts are covered.

**Early exit**: after any sub-run where `saved_count=0`, the flow queries the
API to check if any prompts are still outstanding. If none remain (i.e. other
workers claimed them all), the loop exits immediately. This prevents wasted
iterations when multiple workers are running in parallel.

**Consecutive failure guard**: if two consecutive sub-runs both have
`saved_count=0` and `failed_count>0`, the loop stops with
`stopped_reason=consecutive_all_failed`. This prevents runaway loops when
ChatGPT is actively blocking.

**Mop-up pass** (single-worker runs only): after the main loop, the flow
re-checks remaining prompts and runs additional sub-runs for any that were
skipped. The mop-up pass is skipped when `max_prompts` is set (i.e. when
dispatched by the dispatcher, which handles coverage itself).

---

## 8. Observability

The Prefect UI shows per-run:
- All parameters
- Task-level logs and durations
- Final result summary

Example flow result:

```json
{
  "status": "completed",
  "loaded_count": 2,
  "attempted_count": 1,
  "saved_count": 1,
  "skipped_count": 1,
  "failed_count": 0,
  "batch_id": "fd0c7273-...",
  "brand_id": "adadff95-...",
  "product_output_processing": { "saved_count": 5 },
  "entity_output_processing": { "saved_count": 14 },
  "prompt_output_processing": { "updated_count": 1 },
  "score_workflow_trigger": { "triggered_count": 1 }
}
```

---

## 9. Troubleshooting

### `No module named 'prefect'`

```bash
make install
```

### `No module named 'automated_extraction'`

Install in editable mode and re-register:

```bash
make install
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

### Worker does not pick up runs

Check the work pool name and concurrency limit:

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect work-pool inspect prompt-extraction-uk
```

If concurrency is too low (e.g. still set to 1):

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect work-pool set-concurrency-limit prompt-extraction-uk 9
```

### Flow run stays in `Pending`

The Prefect worker on the target machine may not be running. Check machine
state and logs:

```bash
fly machine list -a prompt-extractor-uk
fly logs -a prompt-extractor-uk --machine <machine-id>
```

### ChatGPT extraction times out

See [GPT_WORKERS.md — Cloudflare challenge](./GPT_WORKERS.md#cloudflare-are-you-human-challenge)
and [GPT_WORKERS.md — Session expiry](./GPT_WORKERS.md#session-expiry).

### Sources not captured

Run with a debug pause:

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'chatgpt-extraction/chatgpt-extraction-uk' \
  --param batch_id=<uuid> \
  --param limit=1 \
  --param sources_panel_pause_seconds=60
```

Then VNC in to inspect the Sources panel state.
