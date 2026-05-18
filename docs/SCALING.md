# Dynamic Worker Scaling

This document covers how to scale Fly.io worker machines up and down for batch extraction workloads — both manually via Prefect deployments and automatically through the dispatcher.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How Scaling Works](#how-scaling-works)
  - [Scale-up](#scale-up)
  - [Scale-down](#scale-down)
  - [Clone tracking](#clone-tracking)
- [Normal Operating State](#normal-operating-state)
- [Running a Large Batch](#running-a-large-batch)
  - [Option A — Manual scaling](#option-a--manual-scaling)
  - [Option B — Auto-scaling via dispatcher](#option-b--auto-scaling-via-dispatcher)
- [Deployments Reference](#deployments-reference)
- [One-time Setup](#one-time-setup)
- [Volumes and Chrome Profiles](#volumes-and-chrome-profiles)
- [Cost Considerations](#cost-considerations)
- [Troubleshooting](#troubleshooting)

---

## Overview

By default only **1 worker** needs to be active. When a large batch comes in, you scale up to as many machines as needed (up to 20+), run the batch, then scale back down.

Previously this was done manually — logging into Fly.io and starting/stopping individual machines. Now it is handled by two Prefect deployments (`scale-workers` and `scale-workers-down`) that call the Fly.io Machines API, and optionally by the dispatcher itself when `auto_scale=True`.

---

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │         Prefect Server               │
                        │  (prompt-extractor-prefect.fly.dev)  │
                        └───────────────┬──────────────────────┘
                                        │ work pool: prompt-extraction-uk
                                        │ concurrency limit = N (updated dynamically)
                            ┌───────────┴──────────────┐
                            │                          │
                   ┌────────▼────────┐       ┌────────▼────────┐
                   │  Original       │  ...  │  Clone          │
                   │  machine        │       │  machine        │
                   │  (permanent)    │       │  (ephemeral)    │
                   │                 │       │                 │
                   │ prefect worker  │       │ prefect worker  │
                   │ --limit 1       │       │ --limit 1       │
                   └─────────────────┘       └─────────────────┘

                   ← up to 4 originals →  ← up to N clones →
```

The Prefect work pool **concurrency limit** is always kept in sync with the number of running machines. This is what gates how many flows run simultaneously — if the limit is lower than the machine count, some workers sit idle; if it is higher, some flows queue.

---

## How Scaling Works

### Scale-up

`fly_scaler.scale_up(app_name, target_count)` runs the following logic:

1. **List all machines** on the Fly.io app via the Machines API.
2. **Start stopped originals** — the 4 permanent machines may be stopped to save costs. They are started first, cheapest way to add capacity.
3. **Clone** — if more machines are still needed, clone a running original. Each clone:
   - Copies the source machine's Docker image, env vars, VM spec, and process command.
   - Has the volume mount stripped (volumes are single-attach and cannot be shared).
   - Gets `FLY_CLONE_LABEL=<timestamp>-<index>` injected into its env (used for tracking).
   - Starts immediately.
4. **Wait** — a configurable pause (default 30 s) lets newly booted Prefect workers register with the server before flows are submitted.
5. **Update concurrency** — the Prefect work pool concurrency limit is set to `total_running`.

### Scale-down

`fly_scaler.scale_down(app_name, keep_count)` runs the following logic:

1. **Destroy all clones** — any machine with `FLY_CLONE_LABEL` in its env is destroyed. This is safe to run multiple times; if a clone was already destroyed it is simply skipped.
2. **Stop excess originals** — if more than `keep_count` originals are running, the extras are stopped (not destroyed — they remain available for the next scale-up).
3. **Update concurrency** — the Prefect work pool concurrency limit is reset to `keep_count`.

### Clone tracking

Clones are identified by the presence of `FLY_CLONE_LABEL` in their machine config env. Original machines deployed via `fly deploy` never have this key. This means:

- Scale-down is **always safe** to run — it will never accidentally destroy an original machine.
- If the scaler crashes mid-scale-up, a follow-up `scale-workers-down` run will clean up any partial clones.
- The tracking requires no external state (no database, no file) — the Fly.io API is the source of truth.

---

## Normal Operating State

When no batch is running, keep **1 machine running** and the rest stopped:

| App | Running | Stopped | Work pool concurrency |
|---|---|---|---|
| `prompt-extractor-uk` | 1 | 3 | 1 |
| `prompt-extractor-us` | 1 | 3 | 1 |

To reach this state after a batch:

```bash
prefect deployment run 'scale-workers-down/scale-workers-down-uk' \
  --param region=uk \
  --param keep_count=1
```

---

## Running a Large Batch

### Option A — Manual scaling

Use this when you want explicit control over the number of machines before triggering work.

**Step 1 — Scale up**

```bash
# Scale to 8 machines on the UK pool
prefect deployment run 'scale-workers/scale-workers-uk' \
  --param target_count=8 \
  --param region=uk
```

Wait for the flow to complete (roughly `wait_for_workers_seconds`, default 30 s, plus machine boot time). You can monitor in the Prefect UI.

**Step 2 — Dispatch the batch**

```bash
prefect deployment run 'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<your-batch-id> \
  --param extraction_type=google-ai-overview \
  --param worker_count=8 \
  --param region=uk \
  --param use_proxy=true \
  --param limit=5 \
  --param delay_seconds=60
```

**Step 3 — Monitor**

Watch the Prefect UI at `https://prompt-extractor-prefect.fly.dev`. Each of the 8 batch flows will pick up work from its assigned offset.

**Step 4 — Scale back down**

Once all flows are complete (or you want to stop early):

```bash
prefect deployment run 'scale-workers-down/scale-workers-down-uk' \
  --param region=uk \
  --param keep_count=1
```

---

### Option B — Auto-scaling via dispatcher

The dispatcher can scale machines automatically by passing `auto_scale=True`. It calculates `effective_workers` (capped at remaining prompt count), scales up to that number, then submits the flows.

Workers are staggered by `stagger_seconds` (default 15) — worker 0 starts immediately, worker 1 waits 15 s, worker 19 waits 4 m 45 s. This prevents all Chrome instances from hitting Google at the same moment, which triggers rate-limiting.

```bash
prefect deployment run 'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<your-batch-id> \
  --param extraction_type=google-ai-overview \
  --param worker_count=20 \
  --param region=uk \
  --param use_proxy=true \
  --param auto_scale=true \
  --param scale_wait_seconds=30 \
  --param stagger_seconds=15
```

The dispatcher:
1. Counts remaining prompts for the batch.
2. Caps `effective_workers` at `min(worker_count, remaining_count)`.
3. Calls `scale_up(app_name, effective_workers)` — starts/clones machines and updates concurrency.
4. Waits `scale_wait_seconds` for workers to connect.
5. Submits one batch flow per worker and exits.

**You still need to scale back down manually** after the batch completes — the dispatcher exits immediately after scheduling flows, so it cannot wait for them to finish.

```bash
prefect deployment run 'scale-workers-down/scale-workers-down-uk' \
  --param region=uk \
  --param keep_count=1
```

---

## Deployments Reference

| Deployment | Purpose | Key parameters |
|---|---|---|
| `scale-workers/scale-workers-uk` | Scale up UK machines | `target_count`, `region`, `wait_for_workers_seconds` |
| `scale-workers-down/scale-workers-down-uk` | Scale down UK machines | `region`, `keep_count` |
| `dispatch-extraction/dispatch-extraction-uk` | Dispatch batch across N workers | `batch_id`, `extraction_type`, `worker_count`, `auto_scale`, `scale_wait_seconds`, `stagger_seconds` |

US equivalents use the `-us` suffix and `region=us`.

### `scale-workers` parameters

| Parameter | Default | Description |
|---|---|---|
| `target_count` | `4` | Number of machines to have running after scale-up |
| `region` | `"uk"` | `"uk"` or `"us"` |
| `work_pool` | auto | Prefect work pool to update. Defaults to `prompt-extraction-{region}` |
| `wait_for_workers_seconds` | `30` | Seconds to wait after cloning for new workers to connect |

### `scale-workers-down` parameters

| Parameter | Default | Description |
|---|---|---|
| `region` | `"uk"` | `"uk"` or `"us"` |
| `keep_count` | `1` | Number of original machines to leave running |
| `work_pool` | auto | Prefect work pool whose concurrency limit to reset |

---

## One-time Setup

The scaler requires a Fly.io API token to call the Machines API. This is already configured, but if you need to rotate it or set it on a new app:

```bash
# Create a long-lived deploy token scoped to the app
flyctl tokens create deploy -a prompt-extractor-uk --expiry 8760h

# Set it as a secret so it is available inside the running machines
flyctl secrets set FLY_API_TOKEN=<token> -a prompt-extractor-uk
flyctl secrets set FLY_API_TOKEN=<token> -a prompt-extractor-us
```

If `FLY_API_TOKEN` is not set, the scaler raises a `RuntimeError` with instructions. Extraction flows themselves do not require this token — only the scaler and dispatcher flows do.

**Optional env vars** (set as Fly secrets if you want to override the defaults):

| Variable | Default | Description |
|---|---|---|
| `FLY_APP_NAME_UK` | `prompt-extractor-uk` | UK Fly.io app name |
| `FLY_APP_NAME_US` | `prompt-extractor-us` | US Fly.io app name |

---

## Volumes and Chrome Profiles

The original UK machines mount a persistent volume (`prompt_extractor_data_uk`) at `/data`. The Chrome user profile is stored at `/data/chrome-profile`. This means:

- **Originals** retain their Chrome profile across runs — cookies, cached pages, login state.
- **Clones** cannot share the volume (Fly.io volumes are single-attach). They use `/tmp/chrome-profile` which is ephemeral and reset on every run.

In practice this matters for **ChatGPT extraction** (`extraction_type=chatgpt`) where login state is stored in the Chrome profile. For Google extraction types (`google-ai-overview`, `google-ai-mode`), there is no login state and the ephemeral profile on clones works fine.

If you need ChatGPT extraction at scale:

1. Use `auto_login=True` with `login_email` set — each clone will log in automatically on startup.
2. Or restrict auto-scaling to Google extraction types and run ChatGPT only on the original machines.

---

## Cost Considerations

Cloned machines are billed by the second at the same rate as originals (2 GB RAM, 1 performance CPU). A rough guide:

| Workers | Extra monthly cost (if left running 24/7) |
|---|---|
| 4 originals (baseline) | ~$60/month |
| +4 clones | ~$60/month extra |
| +16 clones (total 20) | ~$240/month extra |

In practice, clones only run during batch windows — a 4-hour batch with 20 workers costs less than $1 in additional compute. **Always run `scale-workers-down` when a batch finishes.**

---

## Troubleshooting

### Scale-up fails with "FLY_API_TOKEN environment variable is not set"

The token was not set as a Fly secret. See [One-time Setup](#one-time-setup).

### Clones appear in the Fly.io dashboard after a failed batch

Run `scale-workers-down` — it identifies and destroys all machines with `FLY_CLONE_LABEL` regardless of state.

### New workers are not picking up flows (flows stay "Scheduled")

The Prefect work pool concurrency limit may be lower than the number of running machines. Check with:

```bash
# On a UK machine
python -m prefect work-pool inspect prompt-extraction-uk
```

Then correct it:

```bash
prefect work-pool update prompt-extraction-uk --concurrency-limit <N>
```

Or simply re-run `scale-workers` with the correct `target_count` — it will update the limit automatically.

### Chrome fails to start on a clone ("debug port not ready after 60s")

Clones start with a fresh `/tmp/chrome-profile` and no persistent state. If this error appears consistently on clones but not originals, the machine may be under-resourced. Check that the cloned machine config has the same `vm` spec (2 GB RAM, 1 performance CPU) as the original.

### `scale-workers-down` stops an original I want to keep running

Pass `keep_count=N` where N is the number of originals you want to stay running. The default is `keep_count=1` (one active original, three stopped).

```bash
prefect deployment run 'scale-workers-down/scale-workers-down-uk' \
  --param region=uk \
  --param keep_count=4   # keep all 4 originals running
```
