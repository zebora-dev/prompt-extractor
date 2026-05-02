# Automated ChatGPT Extraction

Standalone automation for pulling prompts from the BrandSight API, running them in ChatGPT, capturing the answer via the ChatGPT copy button, and saving outputs back to the existing `prompt-outputs` API.

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

Edit `.env` and set `BRANDSIGHT_SUPABASE_ANON_KEY`. The current extension value is in:

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
  "sources_panel_pause_seconds": 0
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

1. Loads the batch and brand from `GET /batches`.
2. Loads prompts from `POST /prompts`.
3. Skips prompts already saved for the same prompt, brand, and batch.
4. Opens `https://chatgpt.com`.
5. Creates a fresh chat for each prompt where possible.
6. Sends the prompt and waits for the response to finish.
7. Clicks the latest assistant response copy button.
8. Captures copied markdown, rendered raw HTML, source links, product flyout HTML, and model slug.
9. Saves the output to `POST /prompt-outputs`.
10. Runs the `prompt-output-process` Prefect task, which converts `raw_html` into markdown, compares it with the copied markdown, and updates `response`/`markdown` with missing assets such as images and links.

The saved payload includes top-level `response`, `markdown`, `raw_html`, and `sources` fields. Metadata keeps capture method details, `source_count`, `product_count`, and captured product flyout HTML under `output_metadata.original_metadata.product_extraction.outputs`.

## Notes

- The baseline library referenced by the team, `daily-coding-problem/chatgpt-scraper-lib`, is Selenium-based and uses the same core pattern: browser session, prompt textbox, send button, wait for stop button to disappear, then prefer the copy button over DOM text.
- This local implementation keeps those ideas but uses the BrandSight API payloads directly, so it can run without the extension.
- ChatGPT UI selectors can change. If capture breaks, update `automated_extraction/chatgpt_runner.py`.
