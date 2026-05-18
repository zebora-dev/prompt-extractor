# Prefect Operations Guide

This app runs extraction as Prefect flows, giving us observable runs, parameters, logs, duration, state, retry hooks, and a UI for triggering and inspecting work.

Batch, prompt, and prompt-output create/read/update/exists operations go directly through the Python Supabase client.

## Flows

Ten flows are registered across two regional work pools. UK deployments have a `-uk` suffix.

| Flow | Pool (US) | Pool (UK) |
|---|---|---|
| `prompt-extraction-batch` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `prompt-extraction` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `google-ai-mode-extraction-batch` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `google-ai-mode-extraction` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `google-ai-overview-extraction-batch` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `google-ai-overview-extraction` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `prompt-output-processing` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `dispatch-extraction` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `scale-workers` | `prompt-extraction-us` | `prompt-extraction-uk` |
| `scale-workers-down` | `prompt-extraction-us` | `prompt-extraction-uk` |

## Dispatcher & Scaling Flows

### Dispatching a large batch (primary method)

`dispatch-extraction` is the primary way to run large batches. It counts remaining prompts for a batch, splits them into equal chunks, and submits one batch flow run per worker via the Prefect REST API, then exits immediately.

Always point the CLI at the hosted server by prefixing commands with `PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api`.

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<uuid> \
  --param extraction_type=google-ai-overview \
  --param worker_count=8 \
  --param region=uk \
  --param use_proxy=true \
  --param auto_scale=true
```

`auto_scale=true` triggers automatic Fly.io machine scaling before flows are submitted. Without it you must have the required number of workers already running.

**Worker staggering** — with the default `stagger_seconds=15`, worker 0 starts immediately, worker 1 after 15 s, worker 2 after 30 s, and so on. For 20 workers this spreads startup over ~5 minutes, preventing all Chrome instances from hitting Google simultaneously and triggering rate-limiting.

### Scaling up manually

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'scale-workers/scale-workers-uk' \
  --param target_count=8 \
  --param region=uk
```

### Scaling down after a batch

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run 'scale-workers-down/scale-workers-down-uk' \
  --param region=uk \
  --param keep_count=1
```

The ChatGPT flow is:

```text
prompt-extraction
```

The main task is:

```text
extract-chatgpt-batch
```

The task wraps one full extraction run so a single logged-in Chrome profile can process a batch without reopening the browser for every prompt.

After outputs are saved, the flow runs a second task:

```text
product-output-process
```

This task persists captured product flyouts into `prompts_outputs_products`.
Each row includes identifiers for the prompt output, brand, batch, and prompt,
plus `raw_html`, generated `markdown`, `links`, `images`, counts, button index,
and capture method.

The flow then runs:

```text
entity-output-process
```

This task persists captured ChatGPT entity flyouts into
`prompts_outputs_entities`. Each row includes identifiers for the prompt output,
brand, batch, and prompt, plus `entity_text`, `raw_html`, generated `markdown`,
`links`, `images`, counts, entity index, and capture method.

The flow then runs:

```text
prompt-output-process
```

This task converts each saved `raw_html` response into markdown, compares it with
the copied markdown, and enriches the saved `response`/`markdown` with missing
assets such as images and links.

You can also run that processor without re-running ChatGPT extraction via:

```text
prompt-output-processing
```

The final extraction-flow task is:

```text
trigger-score-workflow
```

It posts each newly saved `prompts_outputs.id` to
`BRANDSIGHT_SCORE_WORKFLOW_URL` with
`{batch_id, output_id, force:false, force_run:false}`.

`output_id` is sent as a string for single-output scoring. `scorer_types` is omitted unless
`BRANDSIGHT_SCORE_WORKFLOW_SCORER_TYPES` is configured with one or more comma-separated values.

## 1. Install Dependencies

```bash
cd automated-extraction
source .venv/bin/activate
make install
```

Check Prefect is available:

```bash
python -c "import prefect; print(prefect.__version__)"
```

If `make prefect-server` says `Prefect is not installed in this environment`, install dependencies into the active venv:

```bash
python -m pip install -r requirements.txt
```

If your shell is not picking up the venv `python`, pass it explicitly:

```bash
make PYTHON=.venv/bin/python prefect-server
```

## 2. Configure Environment

Create `.env` if needed:

```bash
cp .env.example .env
```

Required:

```text
BRANDSIGHT_SUPABASE_ANON_KEY=...
```

Prompt-output storage:

```text
BRANDSIGHT_SUPABASE_URL=https://hmwgplzdzffivawkflci.supabase.co
BRANDSIGHT_PROMPT_OUTPUTS_TABLE=prompts_outputs
BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE=prompts_outputs_products
BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE=prompts_outputs_entities
BRANDSIGHT_SCORE_WORKFLOW_URL=https://workflow.zebora.io/api/workflows/score-single-output
WORKFLOW_API_KEY=
BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN=false
BRANDSIGHT_SCORE_WORKFLOW_SCORER_TYPES=
```

`BRANDSIGHT_SUPABASE_URL` is optional when `BRANDSIGHT_API_BASE_URL` is set to the project functions URL; the app derives the project URL automatically. `BRANDSIGHT_API_BASE_URL` is retained as a legacy compatibility setting.

Recommended local Prefect settings:

```text
PREFECT_API_URL=http://localhost:4200/api
PREFECT_WORK_POOL=prompt-extraction-pool
```

Make sure ChatGPT login is prepared before running real extraction:

```bash
make login
```

This stores the browser session in `.chrome-profile` by default.

## 3. Start a Local Prefect Server

Terminal 1:

```bash
make prefect-server
```

Open the UI:

```text
http://localhost:4200
```

You can also use Docker:

```bash
docker compose up prefect-server
```

## 4. Option A: Serve the Flow Locally

This is the easiest development mode. It registers and runs the deployment in the same Python process.

Terminal 2:

```bash
make prefect-serve
```

You should see the `prompt-extraction/prompt-extraction` deployment in the Prefect UI.

### Trigger from the Prefect UI

1. Open `http://localhost:4200`.
2. Go to **Deployments**.
3. Select `prompt-extraction/prompt-extraction`.
4. Click **Quick run** or **Custom run**.
5. Set parameters, for example:

```json
{
  "batch_id": "b4cfbc28-a046-497f-8944-65fcf10d59fe",
  "prompts_file": null,
  "brand_id": null,
  "limit": 2,
  "skip": 0,
  "dry_run": false,
  "headless": null,
  "chrome_user_data_dir": null,
  "sources_panel_pause_seconds": 0,
  "force_rerun": false,
  "llm_model_filter": "gpt"
}
```

### Trigger from the Prefect CLI

```bash
prefect deployment run 'prompt-extraction/prompt-extraction' \
  --param batch_id=b4cfbc28-a046-497f-8944-65fcf10d59fe \
  --param limit=2
```

The extraction flow filters prompts before applying `limit`. By default it only
treats a prompt as completed when an existing `prompts_outputs.llm_model`
contains `gpt`:

```bash
prefect deployment run 'prompt-extraction/prompt-extraction' \
  --param batch_id=b4cfbc28-a046-497f-8944-65fcf10d59fe \
  --param limit=2 \
  --param llm_model_filter=gpt
```

Use an empty `llm_model_filter` to treat any existing model as completed.

To run a prompt even when an output already exists for the same batch, brand,
and prompt:

```bash
prefect deployment run 'prompt-extraction/prompt-extraction' \
  --param batch_id=b4cfbc28-a046-497f-8944-65fcf10d59fe \
  --param limit=1 \
  --param force_rerun=true
```

To re-process existing saved prompt outputs without running ChatGPT extraction:

```bash
prefect deployment run 'prompt-output-processing/prompt-output-processing' \
  --param output_id=9170
```

Or process the latest outputs for a batch:

```bash
prefect deployment run 'prompt-output-processing/prompt-output-processing' \
  --param batch_id=b4cfbc28-a046-497f-8944-65fcf10d59fe \
  --param limit=1
```

For a dry run:

```bash
prefect deployment run 'prompt-extraction/prompt-extraction' \
  --param batch_id=b4cfbc28-a046-497f-8944-65fcf10d59fe \
  --param dry_run=true
```

## 5. Option B: Deploy to Process Work Pools (multi-region)

Use this mode when you want the server and worker lifecycle separated. Two regional work pools route work to the correct Fly.io worker.

### Create work pools (one-time)

```bash
make prefect-pool     # creates prompt-extraction-us
make prefect-pool-uk  # creates prompt-extraction-uk
```

### Register deployments

Run these from your local machine, pointing at the hosted Prefect server:

```bash
# US deployments (10 flows, no suffix, tagged region:us)
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-us \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-us

# UK deployments (10 flows, -uk suffix, tagged region:uk)
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-uk
```

> **⚠️ Always set `PREFECT_WORKING_DIR=/app` when registering against the remote server.**
> Without it, `register_deployments.py` falls back to the local repo path (e.g. `/Users/.../automated-extraction`) and the Fly.io worker will crash with `FileNotFoundError` when it tries to start a flow run — the local path does not exist on the remote machine.
>
> If you run the script directly instead of via `make`, include it explicitly:
>
> ```bash
> PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
> PREFECT_WORK_POOL=prompt-extraction-uk \
> PREFECT_WORKING_DIR=/app \
>   python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
> ```

### Start workers

Workers are managed by Fly.io and start automatically. To run a worker locally for development:

```bash
make prefect-worker     # polls prompt-extraction-us
make prefect-worker-uk  # polls prompt-extraction-uk
```

The worker process must have:

- this repo checked out
- dependencies installed
- `.env` configured
- access to a logged-in Chrome profile
- a display/browser environment if running non-headless

## 6. Run the Flow Directly in Python

Useful for quick debugging without a deployment:

```bash
python - <<'PY'
from automated_extraction.workflows.flows import prompt_extraction_flow

prompt_extraction_flow(
    batch_id="b4cfbc28-a046-497f-8944-65fcf10d59fe",
    limit=2,
)
PY
```

## 7. Parameters

### Parameters — Dispatcher

| Parameter | Default | Description |
|---|---|---|
| `batch_id` | required | BrandSight batch UUID |
| `extraction_type` | required | `"google-ai-overview"`, `"google-ai-mode"`, or `"chatgpt"` |
| `worker_count` | `4` | Max workers to dispatch across |
| `region` | `"uk"` | `"uk"` or `"us"` |
| `limit` | `5` | Prompts per inner extraction run |
| `delay_seconds` | `60` | Seconds between inner runs on each worker |
| `use_proxy` | `false` | Route Chrome through the regional proxy |
| `auto_scale` | `false` | Auto scale Fly.io machines before dispatching |
| `scale_wait_seconds` | `30` | Seconds to wait after scaling for workers to connect |
| `stagger_seconds` | `15` | Per-worker startup delay (worker i sleeps i × stagger_seconds) |

### Parameters — ChatGPT / Google extraction flows

| Parameter | Type | Description |
| --- | --- | --- |
| `batch_id` | string/null | BrandSight batch UUID to load prompts from. |
| `prompts_file` | string/null | Local JSON prompt file path. Use instead of `batch_id`. |
| `brand_id` | string/null | Optional brand UUID override for local prompt files. |
| `limit` | integer/null | Maximum prompts to run. |
| `skip` | integer | Number of loaded prompts to skip. |
| `dry_run` | boolean | Load prompts and log a preview without opening ChatGPT. |
| `headless` | boolean/null | Override `CHATGPT_HEADLESS`; null uses `.env`. |
| `chrome_user_data_dir` | string/null | Override Chrome profile path; null uses `.env`/default. |
| `sources_panel_pause_seconds` | integer | Debug pause after opening Sources. Default is `0`. |

One of `batch_id` or `prompts_file` is required.

## 8. Observability

Prefect captures:

- flow and task parameters
- logs from extraction milestones
- task status and duration
- final result summary
- exception traces

The extraction logs include milestones such as:

```text
Markdown copied from ChatGPT response
Raw HTML extracted from ChatGPT response
Detected ChatGPT response model
Sources button found; opening Sources panel
Sources panel opened
Sources panel links loaded
Sources copied from panel
Found product select button(s); opening product flyouts
Product flyout captured
Capture summary for prompt
Finished prompt output process task
```

The flow returns a summary shaped like:

```json
{
  "status": "completed",
  "loaded_count": 2,
  "attempted_count": 2,
  "saved_count": 2,
  "skipped_count": 0,
  "failed_count": 0,
  "batch_id": "...",
  "brand_id": "...",
  "failures": []
}
```

## 9. Troubleshooting

### `No module named 'prefect'`

Install dependencies in the active environment:

```bash
make install
```

### `No module named 'automated_extraction'`

The worker can see Prefect, but it cannot import this app package. Install the
package in editable mode and redeploy so the worker receives the project
working directory:

```bash
make install
make prefect-deploy
```

Then restart the worker:

```bash
make prefect-worker
```

### Deployment does not appear in UI

Confirm the server is running:

```bash
prefect config view
curl http://localhost:4200/api/health
```

Then serve or deploy again:

```bash
make prefect-serve
```

### Worker does not pick up runs

Check the work pool names match:

```bash
echo $PREFECT_WORK_POOL
prefect work-pool ls
```

The default pool is:

```text
prompt-extraction-pool
```

### ChatGPT opens but is not logged in

Refresh the login profile:

```bash
make login
```

### Sources are visible but not captured

Run with verbose logging:

```bash
python -m automated_extraction --batch-id <batch-id> --limit 1 --verbose
```

Or use a short debug pause:

```bash
python -m automated_extraction --batch-id <batch-id> --limit 1 --sources-panel-pause-seconds 30
```

The logs should show whether the button, panel, links, or extraction step failed.

### Worker does not pick up UK runs

Make sure the deployment name has the `-uk` suffix and the run was submitted against the `prompt-extraction-uk` pool. Check that the UK worker is running and polling the correct pool:

```bash
fly status -a prompt-extractor-uk
fly logs -a prompt-extractor-uk
```

### Google batch stops with `stopped_reason=google_blocked_consecutive`

The worker's datacenter IP is being blocked by Google. Re-trigger the same batch with `use_proxy=True`:

```bash
prefect deployment run 'google-ai-mode-extraction-batch/google-ai-mode-extraction-batch-uk' \
  --param batch_id=<uuid> \
  --param use_proxy=true
```

This requires `GOOGLE_PROXY_URL` to be set as a secret on the worker. See [MULTI_REGION.md](MULTI_REGION.md) for proxy setup details.
