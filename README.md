# Automated ChatGPT Extraction

Standalone automation for pulling batches/prompts from Supabase, running them in ChatGPT, capturing the answer via the ChatGPT copy button, and saving outputs directly back to Supabase.

This mirrors the Chrome/Firefox extension flow in `chromeApp/extension-shared/background.js`, but can run either as a CLI process or as a Prefect-observed workflow.

## Setup

```bash
cd automated-extraction
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Python 3.12 is recommended because `undetected-chromedriver` currently imports `distutils`, which Python 3.13 removed. If you run on Python 3.13, the automation falls back to Selenium's standard Chrome driver.

Edit `.env` and set `BRANDSIGHT_SUPABASE_ANON_KEY`. Prompt-output CRUD uses the Supabase table configured by `BRANDSIGHT_PROMPT_OUTPUTS_TABLE` and defaults to `prompts_outputs`. Product flyout rows use `BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE` and default to `prompts_outputs_products`.

The current extension key value is in:

```text
chromeApp/extension-shared/background.js
```

## Login Session

By default, the automation stores a dedicated logged-in browser profile at:

```text
automated-extraction/.chrome-profile
```

Create or refresh that ChatGPT login session with:

```bash
python -m automated_extraction --login-only
```

The browser will open ChatGPT. Log in manually if prompted. Once the ChatGPT prompt box is visible, the CLI exits and later runs reuse that session.

You can also choose your own dedicated Chrome profile by setting:

```text
CHATGPT_CHROME_USER_DATA_DIR=/absolute/path/to/profile
```

## Run a Batch

```bash
python -m automated_extraction --batch-id <batch-uuid>
```

Useful options:

```bash
python -m automated_extraction --batch-id <batch-uuid> --limit 25 --skip 10
python -m automated_extraction --prompts-file ../chromeApp/extension-shared/prompts.json
python -m automated_extraction --batch-id <batch-uuid> --dry-run
python -m automated_extraction --batch-id <batch-uuid> --limit 1 --force-rerun
python -m automated_extraction --batch-id <batch-uuid> --limit 1 --llm-model-filter gpt
```

## Prefect Orchestration

The CLI execution path is also available as a Prefect flow:

```text
prompt-extraction
```

The flow wraps the current browser automation as one task so a single logged-in Chrome session can process many prompts while Prefect records parameters, logs, state, duration, and failures.

Start a local Prefect server:

```bash
make prefect-server
```

In another terminal, serve the local deployment:

```bash
make prefect-serve
```

The Prefect UI is available at:

```text
http://localhost:4200
```

You can also create a process work pool and register the deployment:

```bash
make prefect-pool
make prefect-deploy
make prefect-worker
```

This registers two deployments:

```text
prompt-extraction/prompt-extraction
prompt-output-processing/prompt-output-processing
```

Trigger parameters mirror the CLI:

```json
{
  "batch_id": "b4cfbc28-a046-497f-8944-65fcf10d59fe",
  "limit": 2,
  "skip": 0,
  "dry_run": false,
  "headless": null,
  "sources_panel_pause_seconds": 0,
  "force_rerun": false,
  "llm_model_filter": "gpt"
}
```

To re-process saved outputs without running ChatGPT again:

```bash
python -m prefect deployment run 'prompt-output-processing/prompt-output-processing' \
  --param output_id=9170
```

or:

```bash
python -m prefect deployment run 'prompt-output-processing/prompt-output-processing' \
  --param batch_id=<batch-uuid> \
  --param limit=1
```

For quick local checks:

```bash
make prefect-list
make dry-run BATCH_ID=<batch-uuid>
make run-batch BATCH_ID=<batch-uuid>
```

For the full setup, trigger, worker, and troubleshooting guide, see:

```text
docs/PREFECT.md
```

## What It Does

1. Loads the batch and brand directly from Supabase table `batches`.
2. Loads active prompts directly from Supabase table `prompts`.
3. Filters to prompts without an existing output for the same prompt, brand, batch, and matching `llm_model`.
4. Opens `https://chatgpt.com`.
5. Creates a fresh chat for each prompt where possible.
6. Sends the prompt and waits for the response to finish.
7. Clicks the latest assistant response copy button.
8. Captures copied markdown, rendered raw HTML, source links, product flyout HTML, entity flyout HTML, and model slug.
9. Saves the output directly to Supabase table `prompts_outputs`.
10. Converts each product flyout `raw_html` into markdown and saves product rows to `prompts_outputs_products`. In Prefect runs this is the observable `product-output-process` task.
11. Converts each entity flyout `raw_html` into markdown and saves entity rows to `prompts_outputs_entities`. In Prefect runs this is the observable `entity-output-process` task.
12. Runs the `prompt-output-process` Prefect task, which converts `raw_html` into markdown, compares it with the copied markdown, and updates `response`/`markdown` with missing assets such as images and links.
13. Triggers the downstream score workflow for each saved `prompts_outputs.id` by posting to `BRANDSIGHT_SCORE_WORKFLOW_URL`.

The saved payload includes top-level `response`, `markdown`, `raw_html`, and `sources` fields. Metadata keeps capture method details, `source_count`, `product_count`, `entity_count`, and extraction summaries under `output_metadata.original_metadata.product_extraction` and `output_metadata.original_metadata.entity_extraction`. Individual product and entity flyouts are saved as rows in `prompts_outputs_products` and `prompts_outputs_entities`. The Supabase layer maps app field `output_metadata` to database column `metadata`.

## Product Table

Create a Supabase table for captured product flyouts:

```sql
create table if not exists prompts_outputs_products (
  id bigserial primary key,
  output_id bigint not null references prompts_outputs(id) on delete cascade,
  brand_id uuid not null,
  batch_id uuid not null,
  prompt_id uuid not null,
  raw_html text,
  markdown text,
  links jsonb not null default '[]'::jsonb,
  images jsonb not null default '[]'::jsonb,
  html_length integer,
  image_count integer,
  text_length integer,
  button_index integer,
  capture_method text,
  created_at timestamptz not null default now()
);

create index if not exists prompts_outputs_products_output_id_idx on prompts_outputs_products(output_id);
create index if not exists prompts_outputs_products_batch_id_idx on prompts_outputs_products(batch_id);
create index if not exists prompts_outputs_products_prompt_id_idx on prompts_outputs_products(prompt_id);
```

## Entity Table

Create a Supabase table for captured entity flyouts:

```sql
create table if not exists prompts_outputs_entities (
  id bigserial primary key,
  output_id bigint not null references prompts_outputs(id) on delete cascade,
  brand_id uuid not null,
  batch_id uuid not null,
  prompt_id uuid not null,
  entity_text text,
  title text,
  raw_html text,
  markdown text,
  links jsonb not null default '[]'::jsonb,
  images jsonb not null default '[]'::jsonb,
  html_length integer,
  image_count integer,
  text_length integer,
  entity_index integer,
  capture_method text,
  created_at timestamptz not null default now()
);

create index if not exists prompts_outputs_entities_output_id_idx on prompts_outputs_entities(output_id);
create index if not exists prompts_outputs_entities_batch_id_idx on prompts_outputs_entities(batch_id);
create index if not exists prompts_outputs_entities_prompt_id_idx on prompts_outputs_entities(prompt_id);
```

## Downstream Scoring Workflow

After extraction and post-processing complete, each saved prompt output triggers:

```text
POST https://workflow.zebora.io/api/workflows/score-single-output
```

Payload:

```json
{
  "batch_id": "<batch-id>",
  "output_id": 5797,
  "force": false,
  "force_run": false,
  "scorer_types": []
}
```

Configure the endpoint and optional API key with:

```text
BRANDSIGHT_SCORE_WORKFLOW_URL=https://workflow.zebora.io/api/workflows/score-single-output
WORKFLOW_API_KEY=
BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN=false
BRANDSIGHT_SCORE_WORKFLOW_SCORER_TYPES=
```

## Notes

- The baseline library referenced by the team, `daily-coding-problem/chatgpt-scraper-lib`, is Selenium-based and uses the same core pattern: browser session, prompt textbox, send button, wait for stop button to disappear, then prefer the copy button over DOM text.
- This local implementation keeps those ideas but uses the BrandSight Supabase tables directly, so it can run without the extension.
- ChatGPT UI selectors can change. If capture breaks, update `automated_extraction/chatgpt_runner.py`.
