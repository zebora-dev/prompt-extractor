# Perplexity.ai Scraper — Architecture & Operations Guide

## Overview

The Perplexity scraper automates prompt submission to perplexity.ai via a pre-authenticated
Chrome browser session (Selenium + undetected-chromedriver).  It shares the same
Prefect / Fly.io / Supabase infrastructure as the ChatGPT and Claude scrapers, and outputs
records into the same `prompt_outputs` table, distinguished by `llm_model` values prefixed
with `perplexity-`.

---

## How it works

### 1. Browser automation (`automated_extraction/perplexity_runner.py`)

`PerplexityRunner` controls Chrome via Selenium with undetected-chromedriver.  Key steps
per prompt:

1. **Navigate to a fresh chat** — `driver.get("https://www.perplexity.ai/")`.  The root URL
   starts a new query session; no need to navigate to a `/new` path.
2. **Dismiss blocking dialogs** — JS-based atomic find-and-click for cookie banners and
   onboarding modals (avoids `StaleElementReferenceException`).
3. **Type the prompt** — Perplexity uses a standard `<textarea>` with
   `placeholder*="Ask"`.  Plain `send_keys` works after an `ActionChains` click-to-focus.
4. **Click Send** — finds `button[aria-label='Submit']` and clicks via ActionChains.
5. **Wait for completion** — waits for Perplexity's post-answer action buttons to appear.
   The send button stays `disabled` throughout (before *and* after the response), so
   button state cannot be used as a completion signal.  Instead, the runner polls for any
   of `["Helpful", "Not helpful", "Copy", "Rewrite Session"]` buttons, which only appear
   after the full response is rendered.  Falls back to text-stability detection (3 identical
   `.prose` lengths at 2 s intervals) if the action buttons never appear.
6. **Capture response** — reads `innerText` from the last `.prose` element directly in the
   DOM.  No clipboard copy needed.
7. **Extract sources** — clicks the **Links** tab button (Perplexity groups sources into
   "Answer / Links / Images" tabs; the Links tab must be activated before scraping).
   Collects all `<a href>` elements excluding internal perplexity.ai links.
8. **Capture model** — the model selector button only shows "Model" as its DOM text; the
   actual model name is not exposed.  Defaults to `"perplexity-sonar"`.
9. **Cloudflare detection** — the same JS probe as Claude/ChatGPT checks for Turnstile
   challenges (no-op at present; Perplexity does not use Turnstile).

### 2. Pre-authenticated Chrome profile

There is no login flow.  Workers rely on a Chrome profile directory that already has an
active perplexity.ai session (cookies `__Secure-next-auth.session-token` + `next-auth.csrf-token`):

- **Local dev**: `.perplexity-profile/` at the project root (gitignored).
- **Fly.io workers**: `/data/chrome-profile/` on the Fly volume.

To set up a local profile:
```bash
python scripts/setup_perplexity_profile.py   # opens Chrome; log in, then it auto-saves
```

To upload to Fly after deploying a fresh app:
```bash
tar -czf /tmp/perplexity-profile.tar.gz -C . .perplexity-profile
flyctl ssh sftp shell -a prompt-extractor-perplexity-uk
> put /tmp/perplexity-profile.tar.gz /data/perplexity-profile.tar.gz

flyctl ssh console -a prompt-extractor-perplexity-uk -C \
  "bash -c 'tar -xzf /data/perplexity-profile.tar.gz -C /data 2>/dev/null && \
            mv /data/.perplexity-profile /data/chrome-profile && \
            rm /data/perplexity-profile.tar.gz'"
```

### 3. Extraction pipeline (`automated_extraction/extraction.py`)

`run_perplexity_extraction_job()` mirrors the Claude and ChatGPT extraction jobs:

1. `load_prompt_work()` fetches remaining prompts (filtered by `llm_model_filter="perplexity"`
   and optionally by `measurements_filter`).
2. `try_claim_prompt()` acquires a distributed lock.
3. `PerplexityRunner.run_prompt()` submits and captures the response.
4. `build_perplexity_prompt_output()` constructs the DB payload:
   - `config.site = "Perplexity"`
   - `output_metadata.llm_model` = normalised model slug (e.g. `perplexity-sonar`)
5. Saved to `prompt_outputs` via `ApiClient.save_prompt_output()`.

### 4. Measurements filter

All extraction layers accept an optional `measurements_filter` string.  When set, only
prompts whose `measurements` field contains that string (case-insensitive) are processed.

### 5. Prefect flows & tasks

| Name | File | Purpose |
|---|---|---|
| `perplexity-extraction` | `workflows/flows.py` | Single run: one set of prompts, one Chrome session |
| `perplexity-extraction-batch` | `workflows/flows.py` | Loops `perplexity-extraction` until remaining = 0 |
| `extract_perplexity_batch_task` | `workflows/tasks.py` | Prefect task wrapping `run_perplexity_extraction_job` |

### 6. Fly.io deployment

| App | Region | Work pool | Config |
|---|---|---|---|
| `prompt-extractor-perplexity-uk` | lhr (London) | `prompt-extraction-perplexity-uk` | `fly-perplexity-uk.yaml` |

**Initial deploy (UK):**
```bash
flyctl apps create prompt-extractor-perplexity-uk
flyctl volumes create prompt_extractor_perplexity_uk \
  -a prompt-extractor-perplexity-uk -r lhr --size 10 --yes
flyctl deploy -a prompt-extractor-perplexity-uk -c fly-perplexity-uk.yaml
# upload Chrome profile (see section 2)
```

**Required secrets:**
```bash
flyctl secrets set -a prompt-extractor-perplexity-uk \
  BRANDSIGHT_API_BASE_URL=... \
  BRANDSIGHT_SUPABASE_URL=... \
  BRANDSIGHT_SUPABASE_ANON_KEY=... \
  BRANDSIGHT_SUPABASE_SERVICE_KEY=... \
  BRANDSIGHT_PROMPT_OUTPUTS_TABLE=... \
  BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE=... \
  BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE=... \
  BRANDSIGHT_SCORE_WORKFLOW_URL=... \
  BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN=... \
  PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
  SLACK_BOT_TOKEN=... \
  FLY_API_TOKEN=$(flyctl auth token)
```

**VNC access** (to observe Chrome or debug):
```bash
flyctl proxy 6080 -a prompt-extractor-perplexity-uk
# open http://localhost:6080 in a browser
```

### 7. Registering Prefect deployments

After deploying a new worker or changing flow code:
```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-perplexity-uk \
  python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

This registers `perplexity-extraction-uk` and `perplexity-extraction-batch-uk` against the
`prompt-extraction-perplexity-uk` work pool.

---

## Running an extraction

### Local test
```bash
python scripts/test_perplexity_local.py
```

### Via CLI
```bash
python -m automated_extraction --provider perplexity --batch-id <BATCH_ID>
```

### Via dispatch loop (recommended for production)
```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
python scripts/dispatch_perplexity_loop.py \
  --batch-id <BATCH_ID> \
  --measurements-filter Visibility \
  --region uk \
  --worker-count 1 \
  --limit 5 \
  --poll-interval 600
```

This polls every 10 minutes (configurable), re-dispatches if prompts remain, cleans up
stale/crashed workers, and stops + scales down the Fly app when the batch is complete.

---

## Data model

Outputs land in `prompt_outputs` with:
- `llm_model`: `perplexity-sonar` (default; actual model not exposed by DOM)
- `config.site`: `"Perplexity"`
- `output_metadata.site_used`: `"Perplexity"`
- `sources`: list of `{url, title}` dicts from the Links tab (typically 10–15 per response)

---

## Key differences from Claude / ChatGPT scrapers

| Feature | ChatGPT | Claude | Perplexity |
|---|---|---|---|
| Input element | `<textarea>` | ProseMirror `contenteditable` | `<textarea>` |
| Text entry | `send_keys` | `document.execCommand` | `send_keys` |
| Completion signal | Stop button disappears | Stop button disappears | "Helpful/Not helpful" action buttons appear |
| Response capture | DOM `.markdown` | Clipboard copy | DOM `.prose` last element |
| Sources location | Separate panel | Inline `<a>` links | **Links tab** (must be clicked first) |
| Model field | `gpt-4o` etc. | Normalised from display name | Always `perplexity-sonar` (not exposed) |
| Site field | `"ChatGPT"` | `"Anthropic"` | `"Perplexity"` |

---

## Debugging

A DOM probe script is available for inspecting Perplexity's live page structure:
```bash
python scripts/debug_perplexity_dom.py
```

This submits "What is 2+2?", then dumps all buttons, SVG buttons, prose elements, and
stop/cancel buttons to logs at 5 s and 15 s after submission — useful for verifying
selectors after a Perplexity UI update.
