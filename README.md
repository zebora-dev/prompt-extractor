# BrandSight Automated Extraction

Headless browser automation that runs brand prompts through AI systems (ChatGPT, Google AI Mode, Google AI Overview), captures structured responses, and saves them to Supabase via the BrandSight API. Orchestrated with Prefect and deployed on Fly.io.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Providers](#providers)
  - [ChatGPT](#chatgpt)
  - [Google AI Mode](#google-ai-mode)
  - [Google AI Overview](#google-ai-overview)
- [People Also Ask (PAA) Capture](#people-also-ask-paa-capture)
- [CLI Reference](#cli-reference)
- [Prefect Orchestration](#prefect-orchestration)
- [Fly.io Deployment](#flyio-deployment)
- [Multi-Region & Proxy](#multi-region--proxy)
- [Development](#development)

---

## Overview

Each extraction run:

1. Loads a batch of prompts from the BrandSight API (or a local JSON file)
2. Skips prompts that already have a saved output for the same batch/brand
3. Opens a real Chrome browser (via Selenium / undetected-chromedriver)
4. Runs each prompt through the target AI system
5. Captures the response — preferring clipboard markdown over DOM text
6. Extracts source links and classifies them (`inline`, `citation`, `more_links`)
7. Captures "People Also Ask" suggestions from Google search pages
8. Saves everything to `prompts_outputs` and `prompts_outputs_suggestions` in Supabase
9. Optionally triggers a downstream scoring workflow

---

## Architecture

```
                        Prefect Cloud
                              │
             ┌────────────────┴────────────────┐
             │                                  │
   pool: prompt-extraction-us        pool: prompt-extraction-uk
             │                                  │
             ▼                                  ▼
   prompt-extractor-us (iad)        prompt-extractor-uk (lhr)
   GOOGLE_SEARCH_COUNTRY=US         GOOGLE_SEARCH_COUNTRY=GB
             │                                  │
   ┌─────────┴──────────────────────────────────┴─────────┐
   │  Prefect Worker (Fly.io machine)                      │
   │                                                       │
   │  ┌───────────────────────────────────────────────┐   │
   │  │  Flow: prompt-extraction-batch                │   │
   │  │    └─ Task: extract-chatgpt-batch             │   │
   │  │         └─ ChatGPTRunner (undetected Chrome)  │   │
   │  │              └─ Clipboard / DOM fallback      │   │
   │  └───────────────────────────────────────────────┘   │
   │                                                       │
   │  ┌───────────────────────────────────────────────┐   │
   │  │  Flow: google-ai-mode-extraction              │   │
   │  │    └─ Task: extract-google-ai-mode-batch      │   │
   │  │         └─ GoogleAIModeRunner (udm=50&arv=1)  │   │
   │  │              ├─ Stealth + proxy layer         │   │
   │  │              ├─ Source classification         │   │
   │  │              └─ PAA capture                   │   │
   │  └───────────────────────────────────────────────┘   │
   │                                                       │
   │  ┌───────────────────────────────────────────────┐   │
   │  │  Flow: google-ai-overview-extraction          │   │
   │  │    └─ Task: extract-google-ai-overview-batch  │   │
   │  │         └─ GoogleAIOverviewRunner (organic)   │   │
   │  │              ├─ Stealth + proxy layer         │   │
   │  │              ├─ Show more expansion           │   │
   │  │              └─ PAA capture                   │   │
   │  └───────────────────────────────────────────────┘   │
   └───────────────────────────────────────────────────────┘
            │                           │
            ▼                           ▼
   Supabase (prompts_outputs)    Prefect Cloud (flow state)
   Supabase (prompts_outputs_suggestions)
```

### Key modules

| Module | Purpose |
|---|---|
| `cli.py` | Entry point — `python -m automated_extraction` |
| `config.py` | Settings loaded from env vars / `.env` file |
| `extraction.py` | Job functions for all three providers |
| `chatgpt_runner.py` | Selenium runner for `chatgpt.com` |
| `google_chrome_factory.py` | Shared Chrome driver factory — stealth patches, user-agent rotation, proxy |
| `google_ai_mode_runner.py` | Selenium runner for Google AI Mode (`udm=50`) |
| `google_ai_overview_runner.py` | Selenium runner for organic AI Overview results |
| `google_suggestions_runner.py` | PAA capture (shared across both Google runners) |
| `api_client.py` | Supabase REST client |
| `supabase_prompt_outputs.py` | Typed row serialisation / deserialisation |
| `workflows/flows.py` | Prefect flow definitions |
| `workflows/tasks.py` | Prefect task wrappers |
| `workflows/register_deployments.py` | Deploy / serve flows against work pools (multi-region) |

---

## Quick Start

```bash
# 1. Clone and set up
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e . --no-deps

# 2. Configure
cp .env.example .env
# Edit .env — set at minimum: BRANDSIGHT_SUPABASE_ANON_KEY

# 3. Run a single-prompt dry run
python -m automated_extraction --batch-id <batch-uuid> --limit 1 --dry-run

# 4. Run for real
python -m automated_extraction --batch-id <batch-uuid> --limit 5
```

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `BRANDSIGHT_SUPABASE_ANON_KEY` | Supabase anon key (from `chromeApp/extension-shared/background.js`) |

### API / Storage

| Variable | Default | Description |
|---|---|---|
| `BRANDSIGHT_API_BASE_URL` | `https://hmwgplzdzffivawkflci.supabase.co/functions/v1/api` | Edge Function base URL |
| `BRANDSIGHT_SUPABASE_URL` | Derived from `API_BASE_URL` | Direct Supabase project URL |
| `BRANDSIGHT_PROMPT_OUTPUTS_TABLE` | `prompts_outputs` | Output table name |
| `BRANDSIGHT_PROMPT_OUTPUT_PRODUCTS_TABLE` | `prompts_outputs_products` | Products table |
| `BRANDSIGHT_PROMPT_OUTPUT_ENTITIES_TABLE` | `prompts_outputs_entities` | Entities table |
| `BRANDSIGHT_PROMPT_OUTPUT_SUGGESTIONS_TABLE` | `prompts_outputs_suggestions` | PAA suggestions table |

### ChatGPT

| Variable | Default | Description |
|---|---|---|
| `CHATGPT_URL` | `https://chatgpt.com` | ChatGPT base URL |
| `CHATGPT_CHROME_USER_DATA_DIR` | `.chrome-profile` | Chrome profile for ChatGPT login |
| `CHATGPT_HEADLESS` | `false` | Run headless (set `true` in CI / Fly.io) |
| `CHATGPT_LOGIN_WAIT_SECONDS` | `180` | Manual login timeout |
| `CHATGPT_RESPONSE_TIMEOUT_SECONDS` | `300` | Max wait for ChatGPT response |
| `CHATGPT_SOURCES_PANEL_PAUSE_SECONDS` | `0` | Debug pause after sources panel opens |
| `CHATGPT_AUTO_LOGIN` | `false` | Enable automated login flow |
| `CHATGPT_LOGIN_EMAIL` | — | Email to use with `CHATGPT_ACCOUNTS_B64` |
| `CHATGPT_ACCOUNTS_B64` | — | Base64-encoded `accounts.json` (see [Automated Login](#automated-login-opt-in)) |

### Google

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_SEARCH_URL` | `https://www.google.com/search` | Google search base URL |
| `GOOGLE_CHROME_USER_DATA_DIR` | `.google-chrome-profile` | Chrome profile for Google (falls back to `CHATGPT_CHROME_USER_DATA_DIR`) |
| `GOOGLE_SEARCH_COUNTRY` | — | Country code, e.g. `US`, `GB` |
| `GOOGLE_SEARCH_LANGUAGE` | `en` | Language code, e.g. `en`, `fr` |
| `GOOGLE_AI_MODE_USE_UDM_50` | `true` | Add `udm=50` to trigger AI Mode |
| `GOOGLE_AI_MODE_USE_ARV_1` | `true` | Add `arv=1` to enable advanced AI mode |
| `GOOGLE_PROXY_URL` | — | Residential proxy URL (`http://user:pass@host:port`). Read when `use_proxy=True` is passed to a flow. Set as a Fly secret — do not commit. |

### Scoring workflow

| Variable | Default | Description |
|---|---|---|
| `BRANDSIGHT_SCORE_WORKFLOW_URL` | `https://workflow.zebora.io/...` | Score trigger endpoint |
| `WORKFLOW_API_KEY` | — | API key for scoring webhook |
| `BRANDSIGHT_SCORE_WORKFLOW_FORCE_RUN` | `false` | Re-score already-scored outputs |
| `BRANDSIGHT_SCORE_WORKFLOW_SCORER_TYPES` | — | Comma-separated scorer type filter |

### Prefect

| Variable | Default | Description |
|---|---|---|
| `PREFECT_API_URL` | — | Prefect Cloud or local server URL |
| `PREFECT_WORK_POOL` | `prompt-extraction-pool` | Work pool name |
| `PREFECT_WORKING_DIR` | Project root | Working directory on the worker |

---

## Providers

### ChatGPT

Runs prompts through `chatgpt.com` using a persistent Chrome profile.

```bash
python -m automated_extraction --batch-id <uuid> --limit 10
```

**How it works:**
- Opens ChatGPT and creates a new chat for each prompt
- Waits for the streaming response to complete (stop button disappears)
- Clicks the copy button on the latest assistant response
- Saves markdown, raw HTML, and citation sources

**Automated login** is opt-in — see [below](#automated-login-opt-in).

---

### Google AI Mode

Runs prompts through Google Search with `udm=50&arv=1` (AI Mode). Does not require login.

```bash
python -m automated_extraction --provider google-ai-mode --batch-id <uuid>
```

```bash
# Prefect flow
make prefect-serve
# then trigger google-ai-mode-extraction from the Prefect UI
```

**What is captured:**
- `response` — clipboard markdown if copy succeeds, otherwise DOM text
- `markdown` — raw clipboard text
- `sources` — array of `{url, source, title, description, favicon_url, extraction_source, citation_count}`
  - `extraction_source` is `inline`, `citation`, or `more_links`
- `ai_mode_triggered` — `true` if an AI Mode panel was detected
- `capture_state` — `complete`, `timeout_partial`, `quota_exhausted`, etc.

---

### Google AI Overview

Runs prompts through standard Google Search and captures the organic AI Overview box.

```bash
python -m automated_extraction --provider google-ai-overview --batch-id <uuid>
```

**Differences from AI Mode:**
- URL has no `udm`/`arv` parameters
- Detects the AI Overview box using multiple DOM strategies
- Clicks "Show more" to expand the full answer before capturing
- Records `ai_overview: true/false` in `output_metadata`

---

## People Also Ask (PAA) Capture

After each Google prompt (both AI Mode and AI Overview), the runner captures the "People Also Ask" accordion section from the same search results page.

For each PAA question found (up to 20 per page):
1. Clicks the accordion header with trusted ActionChains events
2. Waits for the answer panel to expand (`aria-expanded="true"`)
3. Clicks the inner "Show more" button if present
4. Extracts the question text, response, and source links via JS
5. Collapses the item before moving to the next

Results are saved to `prompts_outputs_suggestions`:

| Column | Description |
|---|---|
| `output_id` | FK to the parent `prompts_outputs` row |
| `prompt_id` / `brand_id` / `batch_id` | Inherited from the prompt |
| `index` | Position of the question in the PAA list (1-based) |
| `text` | The question text |
| `response` | The expanded answer text |
| `sources` | JSON array of source links |
| `raw_html` | Raw outer HTML of the answer panel |
| `llm_model` | `google-ai-mode` or `google-ai-overview` |
| `capture_method` | `paa_dom_expanded`, `paa_expand_timeout`, `paa_error`, etc. |
| `error` | Non-null if the item failed to expand |

**Database migration:** run [`docs/migrations/create_prompts_outputs_suggestions.sql`](docs/migrations/create_prompts_outputs_suggestions.sql) before deploying.

---

## Automated Login (opt-in)

For environments where manual login is impractical (scheduled Prefect workers), the ChatGPT runner can drive login itself. Supports `basic` (email + password + TOTP) and `google` SSO.

```bash
CHATGPT_AUTO_LOGIN=true
CHATGPT_LOGIN_EMAIL=automation@example.com
CHATGPT_ACCOUNTS_B64=<base64 of accounts.json>
```

**`accounts.json` shape:**

```json
{
  "automation@example.com": {
    "provider": "basic",
    "password": "openai-account-password",
    "secret": { "chatgpt": "BASE32_TOTP_SECRET" }
  }
}
```

Encode it:

```bash
python -c "import base64,json,sys; print(base64.b64encode(json.dumps(json.load(sys.stdin)).encode()).decode())" < accounts.json
```

Warm the session first:

```bash
python -m automated_extraction --login-only --auto-login --login-email automation@example.com
```

---

## CLI Reference

```
python -m automated_extraction [OPTIONS]
```

| Flag | Description |
|---|---|
| `--provider` | `chatgpt` (default), `google-ai-mode`, `google-ai-overview` |
| `--batch-id` | BrandSight batch UUID |
| `--prompts-file` | Local JSON file of prompts (alternative to `--batch-id`) |
| `--limit N` | Max prompts to run in this session |
| `--skip N` | Skip the first N loaded prompts |
| `--dry-run` | Load prompts, print a preview, exit without opening a browser |
| `--force-rerun` | Re-run prompts that already have a saved output |
| `--llm-model-filter` | Override the model filter used to detect existing outputs |
| `--headless` / `--no-headless` | Override `CHATGPT_HEADLESS` |
| `--chrome-user-data-dir` | Override Chrome profile directory |
| `--auto-login` / `--no-auto-login` | Override `CHATGPT_AUTO_LOGIN` |
| `--login-email` | Override `CHATGPT_LOGIN_EMAIL` |
| `--login-only` | Run the login flow once and exit (ChatGPT only) |
| `--google-country` | Override `GOOGLE_SEARCH_COUNTRY` |
| `--google-language` | Override `GOOGLE_SEARCH_LANGUAGE` |
| `--capture-products` | Enable product flyout capture (ChatGPT only) |
| `--capture-entities` | Enable entity flyout capture (ChatGPT only) |
| `--verbose` | Enable debug logging |

---

## Prefect Orchestration

Seven flows are registered, each deployed to both the US and UK work pools (14 deployments total). UK deployments have a `-uk` name suffix.

| Flow | Description |
|---|---|
| `prompt-extraction-batch` | Sequentially chunks a full batch through ChatGPT |
| `prompt-extraction` | Single-run ChatGPT extraction |
| `google-ai-mode-extraction-batch` | Sequentially chunks a batch through Google AI Mode |
| `google-ai-mode-extraction` | Single-run Google AI Mode extraction |
| `google-ai-overview-extraction-batch` | Sequentially chunks a batch through Google AI Overview |
| `google-ai-overview-extraction` | Single-run Google AI Overview extraction |
| `prompt-output-processing` | Re-process existing outputs (markdown conversion, scoring) |

See [docs/PREFECT.md](docs/PREFECT.md) for full parameter reference and trigger examples.

### Local development

```bash
# Start the Prefect server
make prefect-server

# In another terminal — serve all deployments
make prefect-serve

# Open http://localhost:4200 and trigger a flow run
```

### Deploy to workers (multi-region)

```bash
# Create work pools (one-time)
make prefect-pool     # prompt-extraction-us
make prefect-pool-uk  # prompt-extraction-uk

# Register US deployments
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-us \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-us

# Register UK deployments
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-uk
```

---

## Fly.io Deployment

Two regional workers run as Prefect process workers on Fly.io machines with persistent volumes (`/data`) for Chrome profiles and VNC servers for browser inspection.

| App | Config | Region | Work pool |
|---|---|---|---|
| `prompt-extractor-us` | `fly.yaml` | `iad` (Virginia) | `prompt-extraction-us` |
| `prompt-extractor-uk` | `fly-uk.yaml` | `lhr` (London) | `prompt-extraction-uk` |

### Deploy workers

```bash
make deploy-worker-us   # fly deploy -a prompt-extractor-us -c fly.yaml
make deploy-worker-uk   # fly deploy -a prompt-extractor-uk -c fly-uk.yaml
```

### Set secrets (per worker)

```bash
fly secrets set -a prompt-extractor-us \
  BRANDSIGHT_SUPABASE_ANON_KEY="..." \
  PREFECT_API_URL="https://prompt-extractor-prefect.fly.dev/api" \
  WORKFLOW_API_KEY="..." \
  GOOGLE_PROXY_URL="http://login__cr.us:PASSWORD@gw.dataimpulse.com:823"

fly secrets set -a prompt-extractor-uk \
  BRANDSIGHT_SUPABASE_ANON_KEY="..." \
  PREFECT_API_URL="https://prompt-extractor-prefect.fly.dev/api" \
  WORKFLOW_API_KEY="..." \
  GOOGLE_PROXY_URL="http://login__cr.gb:PASSWORD@gw.dataimpulse.com:823"
```

`GOOGLE_PROXY_URL` is only used when a flow run sets `use_proxy=True`. Set it as a secret rather than in the yaml file so credentials are never committed.

### Inspect browser via VNC

Both machines expose a noVNC web client on port 6080:

```bash
fly proxy 6080 -a prompt-extractor-us  # US worker
fly proxy 6080 -a prompt-extractor-uk  # UK worker
# then open http://localhost:6080
```

---

## Multi-Region & Proxy

See [docs/MULTI_REGION.md](docs/MULTI_REGION.md) for the full guide covering:

- How Prefect routes work to the correct regional worker via work pools
- Google bot-detection evasion techniques (undetected-chromedriver, selenium-stealth, CDP override, user-agent rotation, session warmup)
- Residential proxy setup (`use_proxy` flag, `GOOGLE_PROXY_URL`, consecutive failure guard)
- Proxy provider recommendations (DataImpulse, Decodo)
- How to add a new region

---

## Development

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
make install-dev
```

### Quality checks (mirrors CI)

```bash
make lint        # ruff lint
make format      # ruff format check
make typecheck   # mypy (advisory — pre-existing errors exist)
make test        # pytest
make security    # bandit
make ci          # run all of the above
```

### Run tests

```bash
pytest                          # all tests
pytest tests/test_extraction.py # single file
pytest --cov=automated_extraction --cov-report=html
```

### Fix formatting

```bash
make format-fix   # auto-fix ruff lint + format
```

### Adding a new extraction provider

1. Create `automated_extraction/my_provider_runner.py` with a `MyRunner` class and a `MyCapture` dataclass
2. Add `run_my_provider_extraction_job()` and `build_my_provider_prompt_output()` to `extraction.py`
3. Add `extract_my_provider_batch_task()` to `workflows/tasks.py`
4. Add `my_provider_extraction_flow()` to `workflows/flows.py`
5. Register the deployment in `workflows/register_deployments.py`
6. Add `--provider my-provider` to `cli.py`
7. Add `run-my-provider` to `Makefile`
