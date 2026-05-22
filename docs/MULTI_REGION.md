# Multi-Region Workers & Proxy Setup

This document covers the geo-routing architecture, residential proxy support, and bot-detection evasion layer built into the Google extraction runners.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Fly.io Workers](#flyio-workers)
- [Prefect Geo-Routing](#prefect-geo-routing)
- [Deployment Commands](#deployment-commands)
- [Google Bot-Detection Evasion](#google-bot-detection-evasion)
- [Residential Proxy Setup](#residential-proxy-setup)
- [Proxy Providers](#proxy-providers)
- [Running with a Proxy](#running-with-a-proxy)
- [Adding a New Region](#adding-a-new-region)

---

## Overview

Google extraction runs are sensitive to two signals that trigger CAPTCHAs:

1. **IP reputation** — Fly.io datacenter IPs (Anycast ASN) are well-known to Google and are blocked at the IP layer, independent of browser fingerprint.
2. **Browser fingerprint** — automation-related signals (`navigator.webdriver`, missing plugins, synthetic WebGL, headless markers) that distinguish a bot from a real user.

The solution is a layered approach:

- **Evasion layer** (`google_chrome_factory.py`) patches the browser fingerprint so it looks like a real user even on datacenter IPs — reduces blocking frequency but does not eliminate it.
- **Consecutive failure guard** — batch flows stop automatically after 2 consecutive all-failed runs (`stopped_reason="google_blocked_consecutive"`) instead of burning through 70 sub-runs.
- **Residential proxy** (`use_proxy=True`) routes traffic through a residential exit node whose IP is invisible to Google's ASN blocklists. This is the reliable long-term fix.
- **Geo-routing** — each Fly.io region has its own Prefect work pool. UK batches land on the London worker which uses a UK residential exit node; US batches land on the Virginia worker with a US exit node.

---

## Architecture

```
                  Prefect Cloud
                       │
          ┌────────────┴────────────┐
          │                         │
   pool: prompt-extraction-us   pool: prompt-extraction-uk
          │                         │
          ▼                         ▼
 prompt-extractor-us (iad)   prompt-extractor-uk (lhr)
   GOOGLE_SEARCH_COUNTRY=US    GOOGLE_SEARCH_COUNTRY=GB
   GOOGLE_PROXY_URL=...us...   GOOGLE_PROXY_URL=...gb...
          │                         │
          ▼                         ▼
   Deployments (no suffix)    Deployments (-uk suffix)
   prompt-extraction-batch    prompt-extraction-batch-uk
   google-ai-mode-...         google-ai-mode-...-uk
   ...                        ...
```

Prefect routes work to the correct region via **work pools**: each worker polls only its own pool, so a flow run submitted against a `-uk` deployment can only be picked up by the UK worker.

---

## Fly.io Workers

### US worker — `prompt-extractor-us` (Virginia, `iad`)

Config: [`fly.yaml`](../fly.yaml)

| Setting | Value |
|---|---|
| App | `prompt-extractor-us` |
| Region | `iad` (Ashburn, Virginia) |
| Work pool | `prompt-extraction-us` |
| Geo defaults | `GOOGLE_SEARCH_COUNTRY=US`, `GOOGLE_SEARCH_LANGUAGE=en` |
| Volume | `prompt_extractor_data` → `/data` |
| Memory | 2 GB performance CPU |

### UK worker — `prompt-extractor-uk` (London, `lhr`)

Config: [`fly-uk.yaml`](../fly-uk.yaml)

| Setting | Value |
|---|---|
| App | `prompt-extractor-uk` |
| Region | `lhr` (London) |
| Machines | 4 (each runs 1 Prefect worker, `--limit 1`) |
| Work pool | `prompt-extraction-uk` |
| Geo defaults | `GOOGLE_SEARCH_COUNTRY=GB`, `GOOGLE_SEARCH_LANGUAGE=en` |
| Volume | `prompt_extractor_data_uk` (one per machine) → `/data` |
| Memory | 2 GB performance CPU per machine |

### Initial worker setup (one-time)

```bash
# Create 4 volumes (one per machine, 5 GB each)
for i in 1 2 3 4; do
  fly volumes create prompt_extractor_data_uk \
    -a prompt-extractor-uk --region lhr --size 5 --yes
done

# Set required secrets
fly secrets set -a prompt-extractor-uk \
  BRANDSIGHT_SUPABASE_ANON_KEY="..." \
  BRANDSIGHT_API_BASE_URL="..." \
  PREFECT_API_URL="https://prompt-extractor-prefect.fly.dev/api" \
  WORKFLOW_API_KEY="..."

# Deploy the worker image
make deploy-worker-uk

# Scale to 4 machines (Fly auto-assigns one volume per machine)
fly scale count 4 -a prompt-extractor-uk --yes
```

### Deploy commands

```bash
make deploy-worker-us   # fly deploy -a prompt-extractor-us -c fly.yaml
make deploy-worker-uk   # fly deploy -a prompt-extractor-uk -c fly-uk.yaml
```

### Inspect browser via VNC

Both workers expose a noVNC web client on port 6080:

```bash
fly proxy 6080 -a prompt-extractor-us  # US worker
fly proxy 6080 -a prompt-extractor-uk  # UK worker
# then open http://localhost:6080
```

---

## Prefect Geo-Routing

### Work pools

Each region has its own Prefect process work pool. The worker process polls only its own pool:

| Pool | Worker | Region |
|---|---|---|
| `prompt-extraction-us` | `prompt-extractor-us` | Virginia |
| `prompt-extraction-uk` | `prompt-extractor-uk` | London |

Create pools (one-time, against your hosted Prefect server):

```bash
make prefect-pool     # creates prompt-extraction-us
make prefect-pool-uk  # creates prompt-extraction-uk
```

### Deployment naming convention

| Deployment | Pool | Suffix | Region tag |
|---|---|---|---|
| `prompt-extraction-batch` | `prompt-extraction-us` | _(none)_ | `region:us` |
| `google-ai-mode-extraction-batch` | `prompt-extraction-us` | _(none)_ | `region:us` |
| `prompt-extraction-batch-uk` | `prompt-extraction-uk` | `-uk` | `region:uk` |
| `google-ai-mode-extraction-batch-uk` | `prompt-extraction-uk` | `-uk` | `region:uk` |
| _(same pattern for all 7 flows)_ | | | |

All 7 flows exist in both regions (14 deployments total).

---

## Deployment Commands

### Register deployments

Run these from a local machine against the hosted Prefect server:

```bash
# US — re-register / update all 7 US deployments
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-us \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-us

# UK — register / update all 7 UK deployments
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
PREFECT_WORKING_DIR=/app \
  make prefect-deploy-uk
```

### List deployments

```bash
make prefect-list
# or with a region filter:
python -m automated_extraction.workflows.register_deployments --list --region uk
```

### Trigger a UK batch (example)

```bash
prefect deployment run 'google-ai-mode-extraction-batch/google-ai-mode-extraction-batch-uk' \
  --param batch_id=<uuid> \
  --param limit=5
```

Add `--param use_proxy=true` to route through the residential proxy (requires `GOOGLE_PROXY_URL` secret to be set on the worker).

---

## Google Bot-Detection Evasion

The `google_chrome_factory.py` module centralises all Chrome driver construction and stealth patching for both Google runners.

### Techniques applied

| Technique | How |
|---|---|
| **ChromeDriver binary patch** | `undetected_chromedriver` strips automation signatures from the binary |
| **`navigator.webdriver` override** | CDP `Page.addScriptToEvaluateOnNewDocument` injects the override _before_ any page JS runs, so it cannot be detected |
| **Fingerprint patches** | `selenium-stealth` patches `navigator.plugins`, `window.chrome`, WebGL vendor/renderer, permissions API, hairline detection |
| **User-agent rotation** | One of 5 real Chrome 135–137 user-agents (Windows/macOS) is selected at random per session |
| **Window size jitter** | Width and height are randomised within ±20px of 1920×1080 |
| **Session warmup** | Homepage visit before the first search establishes real cookies and a 2–3.5s random delay |

### Driver fallback chain

```
use_proxy=True?
    → selenium-wire + undetected_chromedriver  (proxy auth handled internally)
    → undetected_chromedriver (no proxy, selenium-wire unavailable)
    → plain webdriver.Chrome (last resort, no stealth)

use_proxy=False?
    → undetected_chromedriver
    → plain webdriver.Chrome
```

### Consecutive failure guard

Batch flows (`*-batch` deployments) stop automatically after 2 consecutive sub-runs where every prompt failed. The flow returns `stopped_reason="google_blocked_consecutive"`. Re-trigger with `use_proxy=True` once proxy credentials are set.

---

## Residential Proxy Setup

### The `use_proxy` flag

All Google flows and batch flows accept a `use_proxy: bool` parameter (default `false`). When `true`, the runner calls `resolve_proxy_url(use_proxy)` which reads `GOOGLE_PROXY_URL` from the worker's environment.

**Recommended workflow:**

1. Run without proxy (free). If batch fails with `google_blocked_consecutive`, stop.
2. Re-trigger the same batch with `use_proxy=True` — proxy traffic kicks in only for that run.
3. Once unblocked, you can return to `use_proxy=False` to keep costs down.

### Setting `GOOGLE_PROXY_URL` on the workers

`GOOGLE_PROXY_URL` must be set as a Fly secret (not in `fly.yaml`) so credentials are never committed to source control.

```bash
# US worker — US residential exit
fly secrets set -a prompt-extractor-us \
  GOOGLE_PROXY_URL="http://login__cr.us:PASSWORD@gw.dataimpulse.com:823"

# UK worker — UK residential exit
fly secrets set -a prompt-extractor-uk \
  GOOGLE_PROXY_URL="http://login__cr.gb:PASSWORD@gw.dataimpulse.com:823"
```

### How proxy auth works

Chrome silently ignores credentials embedded in `--proxy-server`. The factory uses `selenium-wire` which intercepts requests at a lower level and injects the `Proxy-Authorization` header, so authenticated proxies work correctly.

---

## Proxy Providers

### DataImpulse (recommended)

- **Pricing**: $1/GB country-level, $2/GB city-level. Traffic never expires.
- **Sign up**: [dataimpulse.com](https://dataimpulse.com)
- **Endpoint format**:

```
# Country-level
http://login__cr.us:PASSWORD@gw.dataimpulse.com:823   # US
http://login__cr.gb:PASSWORD@gw.dataimpulse.com:823   # UK

# City-level (London)
http://login__cr.gb;city.london:PASSWORD@gw.dataimpulse.com:823
```

### Decodo (alternative)

- **Pricing**: Dedicated endpoints, 100MB free trial.
- **Sign up**: [decodo.com](https://decodo.com)
- **Endpoint format**:

```
http://USERNAME:PASSWORD@us.decodo.com:30000           # US
http://USERNAME:PASSWORD@gb.decodo.com:30000           # UK
http://USERNAME:PASSWORD@city.decodo.com:21050         # London city
```

---

## Running with a Proxy

### Via Prefect UI

Set `use_proxy: true` in the flow parameters when triggering a run.

### Via Prefect CLI

```bash
# UK batch with proxy
prefect deployment run 'google-ai-mode-extraction-batch/google-ai-mode-extraction-batch-uk' \
  --param batch_id=<uuid> \
  --param limit=5 \
  --param use_proxy=true

# US batch with proxy
prefect deployment run 'google-ai-mode-extraction-batch/google-ai-mode-extraction-batch' \
  --param batch_id=<uuid> \
  --param limit=5 \
  --param use_proxy=true
```

### Via CLI (local dev)

```bash
# No proxy env var needed for the local flag-based override
GOOGLE_PROXY_URL="http://user:pass@host:port" \
  python -m automated_extraction \
    --provider google-ai-mode \
    --batch-id <uuid> \
    --limit 5
```

---

## Proxy Cost Attribution

Proxy bytes transferred per prompt are tracked automatically and stored in `output_metadata.proxy_usage` for all Google extraction runs when `use_proxy=true`.

### Stored shape

```json
{
  "proxy_usage": {
    "bytes_transferred": 4823041,
    "use_proxy": true,
    "provider": "dataimpulse"
  }
}
```

Bytes are counted at the CONNECT proxy layer (all TLS traffic through the tunnel), not at the application level, so the figure accurately captures all data transferred to/from Google — including page resources, images, and API calls made during the extraction session.

### Per-batch cost query

```sql
SELECT
  batch_id,
  COUNT(*) AS prompt_count,
  SUM((output_metadata->'proxy_usage'->>'bytes_transferred')::bigint) AS total_bytes,
  ROUND(
    SUM((output_metadata->'proxy_usage'->>'bytes_transferred')::bigint)
    / 1e9 * 0.80, 4
  ) AS estimated_cost_usd
FROM prompts_outputs
WHERE output_metadata->'proxy_usage' IS NOT NULL
GROUP BY batch_id
ORDER BY estimated_cost_usd DESC;
```

The `$0.80/GB` multiplier reflects DataImpulse's 1 TB tier. Adjust if on a different tier:

- Under 1 TB: `$1.00/GB` — change `0.80` → `1.00`
- Over 5 TB: contact DataImpulse for a custom rate

Rows created before this feature was deployed will not have `proxy_usage` in their metadata. The `WHERE` clause filters these out automatically.

---

## Adding a New Region

1. Create a new Fly.io app and volume:

```bash
fly apps create prompt-extractor-<region>
fly volumes create prompt_extractor_data_<region> \
  -a prompt-extractor-<region> --region <region-code> --size 5 --yes
```

2. Copy `fly-uk.yaml` → `fly-<region>.yaml` and update `app`, `primary_region`, `PREFECT_WORK_POOL`, and `GOOGLE_SEARCH_COUNTRY`.

3. Add the region to `REGIONS` in [`automated_extraction/workflows/register_deployments.py`](../automated_extraction/workflows/register_deployments.py):

```python
REGIONS = {
    "us": {"suffix": "", "tag": "region:us"},
    "uk": {"suffix": "-uk", "tag": "region:uk"},
    "<region>": {"suffix": "-<region>", "tag": "region:<region>"},
}
```

4. Add `Makefile` targets:

```makefile
prefect-pool-<region>: check-prefect
    PREFECT_WORK_POOL="prompt-extraction-<region>" $(PYTHON) -m automated_extraction.workflows.register_deployments --create-pool

prefect-deploy-<region>: check-prefect
    PREFECT_WORK_POOL="prompt-extraction-<region>" $(PYTHON) -m automated_extraction.workflows.register_deployments --deploy-local --region <region>

deploy-worker-<region>:
    fly deploy -a prompt-extractor-<region> -c fly-<region>.yaml
```

5. Set secrets and deploy:

```bash
fly secrets set -a prompt-extractor-<region> \
  BRANDSIGHT_SUPABASE_ANON_KEY="..." \
  PREFECT_API_URL="..." \
  GOOGLE_PROXY_URL="http://login__cr.<cc>:PASSWORD@gw.dataimpulse.com:823"

make deploy-worker-<region>
make prefect-pool-<region>
make prefect-deploy-<region>
```
