# Claude.ai Scraper — Architecture & Operations Guide

## Overview

The Claude scraper automates prompt submission to Claude.ai via a pre-authenticated Chrome
browser session (Selenium + undetected-chromedriver).  It shares the same Prefect/Fly.io/
Supabase infrastructure as the ChatGPT scraper, and outputs records into the same
`prompt_outputs` table, distinguished by `llm_model` values prefixed with `claude-`.

---

## How it works

### 1. Browser automation (`automated_extraction/claude_runner.py`)

`ClaudeRunner` controls Chrome via Selenium with undetected-chromedriver to avoid bot
detection.  Key steps per prompt:

1. **Navigate to a fresh chat** — `driver.get("https://claude.ai/new")`.
2. **Type the prompt** — Claude uses a ProseMirror `contenteditable` div (not a `<textarea>`).
   Plain `send_keys` silently fails.  The runner uses `document.execCommand('insertText')`
   to insert text in a way the editor accepts.
3. **Click Send** — finds `button[aria-label='Send message']` and clicks via ActionChains.
4. **Wait for completion** — polls for the Stop button (`button[aria-label*='Stop']`) to
   appear (streaming started) then disappear (streaming done).  Falls back to text-stability
   detection if the stop button never appears.
5. **Capture response** — clicks the copy button (`[data-testid='action-bar-copy']`) to put
   the response Markdown into the clipboard via pyperclip.
6. **Extract sources** — scrapes inline `<a href>` links from `div.font-claude-response`.
7. **Capture model** — reads the model selector button text
   (`button[data-testid='model-selector-dropdown']`) and normalises it (e.g.
   "Sonnet 4.6 Low" → "claude-sonnet-4-6").
8. **Cloudflare detection** — a JS probe checks for Turnstile challenges.  If detected, a
   Slack alert is sent and the worker waits up to 10 minutes for the user to solve it via VNC.

### 2. Pre-authenticated Chrome profile

There is no login flow.  Workers rely on a Chrome profile directory that already has an
active Claude.ai session:

- **Local dev**: `.claude-profile/` at the project root.
- **Fly.io workers**: `/data/chrome-profile/` on the Fly volume.

To set up a profile:
```bash
python scripts/setup_claude_profile.py   # opens Chrome; log in, then close it
```

To upload to Fly (after first machine boot):
```bash
fly ssh sftp shell -a prompt-extractor-claude-uk
> put .claude-profile.tar.gz /data/claude-profile.tar.gz
# then SSH in and extract:
fly ssh console -a prompt-extractor-claude-uk
tar -xzf /data/claude-profile.tar.gz -C /data/chrome-profile
rm /data/chrome-profile/SingletonLock /data/chrome-profile/SingletonCookie /data/chrome-profile/SingletonSocket 2>/dev/null || true
```

### 3. Extraction pipeline (`automated_extraction/extraction.py`)

`run_claude_extraction_job()` mirrors `run_extraction_job()` for ChatGPT:

1. `load_prompt_work()` fetches remaining prompts from Supabase (filtered by `llm_model_filter="claude"` and optionally by `measurements_filter`).
2. For each prompt, `try_claim_prompt()` acquires a distributed lock so concurrent workers don't duplicate work.
3. `ClaudeRunner.run_prompt()` submits and captures the response.
4. `build_claude_prompt_output()` constructs the DB payload, normalising the model slug and setting `config.site = "Anthropic"`.
5. The output is saved to `prompt_outputs` via `ApiClient.save_prompt_output()`.
6. On success: `complete_claim()`.  On failure: `release_claim()`.

### 4. Measurements filter

All extraction layers accept an optional `measurements_filter` string.  When set, only
prompts whose `measurements` field contains that string (case-insensitive) are processed.

Example: `measurements_filter="Visibility"` restricts processing to prompts tagged for
Visibility measurement tracking.

### 5. Prefect flows & tasks

| Name | File | Purpose |
|---|---|---|
| `claude-extraction` | `workflows/flows.py` | Single run: one set of prompts, one Chrome session |
| `claude-extraction-batch` | `workflows/flows.py` | Loops `claude-extraction` until remaining = 0 |
| `extract_claude_batch_task` | `workflows/tasks.py` | Prefect task wrapping `run_claude_extraction_job` |
| `dispatch-extraction` | `workflows/dispatcher.py` | Dispatches N workers across a batch |

### 6. Fly.io deployment

| App | Region | Work pool | Config | VM size |
|---|---|---|---|---|
| `prompt-extractor-claude-uk` | lhr (London) | `prompt-extraction-claude-uk` | `fly-claude-uk.yaml` | performance-2x, **8 GB RAM** |
| `prompt-extractor-claude-us` | iad (Washington DC) | `prompt-extraction-claude-us` | `fly-claude-us.yaml` | performance-2x, 8 GB RAM |

> **Why 8 GB?** Chrome + the Prefect process together peak at ~4–5 GB.  If two `prefect.engine`
> processes briefly coexist (e.g. during a redeploy), a 4 GB machine will OOM-kill both.

**Initial deploy (UK example):**
```bash
fly apps create prompt-extractor-claude-uk
fly vol create prompt_extractor_claude_uk -a prompt-extractor-claude-uk -r lhr --size 10
fly deploy -a prompt-extractor-claude-uk -c fly-claude-uk.yaml
```

**Required secrets (set once):**
```bash
fly secrets set -a prompt-extractor-claude-uk \
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
  WORKFLOW_API_KEY=... \
  SLACK_BOT_TOKEN=... \
  FLY_API_TOKEN=$(fly auth token)
```

**VNC access** (to log in or debug Chrome):
```bash
fly proxy 6080 -a prompt-extractor-claude-uk
# open http://localhost:6080 in a browser
```

### 7. Registering Prefect deployments

After deploying a new worker or changing flow code, run from the project root with
`PREFECT_WORKING_DIR` pointing at the **remote** `/app` path:

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORKING_DIR=/app \
  python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

> **Important:** `register_deployments.py` registers *all* flows (ChatGPT, Claude, Perplexity,
> Google) against whatever `PREFECT_WORK_POOL` is set.  After running it, verify that
> each deployment is assigned to its correct pool.  If they end up on the wrong pool, patch
> them individually via the Prefect API:
> ```python
> import httpx
> PREFECT = "https://prompt-extractor-prefect.fly.dev/api"
> # find deployment id from /deployments/filter, then:
> httpx.patch(f"{PREFECT}/deployments/{id}", json={"work_pool_name": "prompt-extraction-claude-uk"})
> ```
>
> Always set `PREFECT_WORKING_DIR=/app` when registering from a local Mac.  Without it,
> Prefect stores the Mac filesystem path in the deployment and the remote worker crashes
> with `FileNotFoundError`.

---

## Running an extraction

### Local test
```bash
python scripts/test_claude_local.py
```

### Via CLI
```bash
python -m automated_extraction --provider claude --batch-id <BATCH_ID>
```

### Via Prefect dispatch (recommended for production)

Trigger from the Prefect UI or API — run the `dispatch-extraction` flow with:
```json
{
  "batch_id": "<BATCH_ID>",
  "extraction_type": "claude",
  "worker_count": 1,
  "region": "uk",
  "limit": 5,
  "measurements_filter": "Visibility"
}
```

### Via dispatch loop script
```bash
python scripts/dispatch_claude_loop.py \
  --batch-id 45c96267-14f0-40c7-bb1d-5850485cef9f \
  --measurements-filter Visibility \
  --region uk
```

This polls every 10 minutes, re-dispatches if prompts remain, cleans up stale workers, and
stops + scales down the Fly app when the batch is complete.

---

## Data model

Outputs land in `prompt_outputs` with:
- `llm_model`: normalised model slug, e.g. `claude-sonnet-4-6`
- `config.site`: `"Anthropic"`
- `output_metadata.site_used`: `"Anthropic"`
- `sources`: list of `{url, title}` dicts extracted from inline links

---

## Operational notes

### Starting and stopping machines

Workers are not free to idle.  Stop them when no batch is running:
```bash
flyctl machine stop <MACHINE_ID> -a prompt-extractor-claude-uk
# later:
flyctl machine start <MACHINE_ID> -a prompt-extractor-claude-uk
```

The dispatch loop script (`dispatch_claude_loop.py`) does this automatically when a batch
completes.

### Chrome Preferences file corruption

Chrome accumulates a `Default/Preferences` file in the profile directory.  After extended
sessions this file can grow to several GB, causing Chrome to crash with SIGTRAP on the next
startup (it tries to parse the giant JSON file at launch).

`ClaudeRunner.start()` automatically deletes `Default/Preferences` if it exceeds 100 MB
before launching Chrome.  Chrome recreates it with defaults; the login session (stored in
cookies and `Network Persistent State`) is unaffected.

If a worker is stuck and manual recovery is needed:
```bash
flyctl ssh console -a prompt-extractor-claude-uk \
  -C "bash -c 'rm -f /data/chrome-profile/Default/Preferences'"
```

### Chrome Singleton lock files

When Chrome crashes (OOM, SIGTRAP, or forceful kill), it leaves `SingletonLock`,
`SingletonCookie`, and `SingletonSocket` files in the profile root.  On the next startup
Chrome refuses to open: *"profile appears to be in use by another process"*.

`ClaudeRunner.start()` automatically removes these files before launching Chrome.

Manual recovery:
```bash
flyctl ssh console -a prompt-extractor-claude-uk \
  -C "bash -c 'rm -f /data/chrome-profile/Singleton*'"
```

### Model name normalisation

Claude's model selector occasionally includes private-use Unicode codepoints
(`U+E000`–`U+F8FF`) in the display name (e.g. `"Sonnet 4.6 Low "`).
`_normalise_claude_model()` strips these before producing the slug, so DB records always
contain clean values like `claude-sonnet-4-6`.

---

## Key differences from ChatGPT scraper

| Feature | ChatGPT | Claude |
|---|---|---|
| Input element | `<textarea>` | ProseMirror `contenteditable` div |
| Text entry | `send_keys` | `document.execCommand('insertText')` |
| Login flow | Optional auto-login | Pre-auth profile only |
| Sources | Separate "Sources" panel | Inline `<a>` links in response |
| Model field | `gpt-4o` etc. | Normalised from display name |
| Site field | `"ChatGPT"` | `"Anthropic"` |
