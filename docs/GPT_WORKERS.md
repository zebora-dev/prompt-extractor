# ChatGPT Extraction Workers — Architecture & Operations

This document describes the dedicated Fly.io worker fleet that runs ChatGPT
(GPT) prompt extractions at scale. It covers how workers are provisioned, how
Chrome profiles are managed, how sessions stay alive between runs, and how to
operate, debug, and extend the fleet.

---

## Overview

ChatGPT extraction cannot use a headless API — it requires a real, logged-in
browser session. To run many prompts in parallel we operate a fleet of
**dedicated Fly.io machines**, one ChatGPT account per machine, with a
persistent Chrome browser that stays alive between extraction runs.

```
┌──────────────────────────────────────────────────────────────┐
│                   Fly.io app: prompt-extractor-uk            │
│                                                              │
│  Machine 0 (dev@theround.com)                                │
│  ├── Chrome on :9222 ──► chatgpt.com (logged in)            │
│  └── Prefect worker ──► polls prompt-extraction-uk pool      │
│                                                              │
│  Machine 1 (chris@theround.com)                              │
│  ├── Chrome on :9222 ──► chatgpt.com (logged in)            │
│  └── Prefect worker ──► polls prompt-extraction-uk pool      │
│                                                              │
│  … 7 more machines (one per account)                         │
└──────────────────────────────────────────────────────────────┘
                        │
                        ▼ picks up flow runs
         Prefect server (prompt-extractor-prefect.fly.dev)
                        │
                        ▼ writes outputs
                  BrandSight API / Supabase
```

---

## Machines & Accounts

9 dedicated machines, each permanently assigned one ChatGPT account.

| UK Worker ID     | UK Worker ID (old)                          | US Worker ID     | Account                  | Profile index |
|------------------|---------------------------------------------|------------------|--------------------------|---------------|
| —                | e829420a634578                              | d8d3160b35de68   | dev@theround.com         | 0             |
| —                | 7849237b673708                              | e829397bdd1038   | chris@theround.com       | 1             |
| —                | e82949df4390d8                              | d8927e5c775e58   | bob@theround.com         | 2             |
| d8d3744c34e4e8   | —                                           | 78452e3b292208   | frank@theround.com       | 3             |
| 7849237b673208   | —                                           | 822e94c79651d8   | info@zebora.io           | 4             |
| —                | e829491b6d4268                              | 781e5d1c6e9128   | dev@zebora.io            | 5             |
| d896d6da5d3938   | —                                           | 6837ee3ce30758   | data@zebora.io           | 6             |
| —                | 0805610f32d018                              | d89590ebed9308   | rob@zebora.io            | 7             |
| —                | 18592e4a677678                              | 8d4e06ced91468   | john@zebora.io           | 8             |

Per-machine env vars (`CHROME_PROFILE_INDEX`, `CHATGPT_LOGIN_EMAIL`) are set
via `fly machine update --env` and are **not** in `fly-uk.yaml` (Fly wipes
per-machine env vars on every deploy — see [Deploying](#deploying)).

---

## Persistent Chrome Architecture

### Why persistent Chrome?

ChatGPT requires an active browser session with cookies and localStorage intact.
Starting a fresh Chrome on every extraction run wastes ~30 s on browser
startup, login verification, and page load — and risks triggering session
challenges. Instead we keep a single Chrome instance alive indefinitely.

### How it works

1. **Xvfb** creates a virtual display (`:99`).
2. **`docker/entrypoint.sh`** starts Chrome pointing at the persistent profile
   on the Fly volume (`/data/chrome-profile`) with the CDP remote debugging
   port open on `9222`.
3. A **watchdog loop** (bash background process) monitors the Chrome PID every
   5 seconds and restarts it if it exits unexpectedly.
4. The **Prefect worker** (`python -m prefect worker start`) polls for flow
   runs and spawns extraction sub-processes.
5. Each extraction sub-process connects to the already-running Chrome via
   `chromedriver debugger_address=localhost:9222` — no browser startup needed.
6. After each extraction run, the Python reference to the browser session is
   released (`driver = None`) **without calling `driver.quit()`** or
   `driver.close()`. Chrome keeps running with all tabs and cookies intact.

### Chrome profile storage

The Chrome profile (`/data/chrome-profile`) lives on a **Fly volume** mounted
at `/data`. Volumes are local to the machine — profiles are never uploaded or
downloaded between runs, and never shared between machines.

```
fly volume list -a prompt-extractor-uk
```

Each machine was created with its own named volume (e.g.
`prompt_extractor_data_chris`) and the volume persists across machine restarts
and deploys.

### Key env vars (app-wide, set in `fly-uk.yaml`)

| Variable                       | Value             | Effect                                      |
|--------------------------------|-------------------|---------------------------------------------|
| `CHATGPT_CHROME_USER_DATA_DIR` | `/data/chrome-profile` | Chrome profile location on the Fly volume |
| `CHATGPT_HEADLESS`             | `false`           | Chrome runs with a virtual display          |
| `CHATGPT_PERSISTENT_CHROME`    | `true`            | Entrypoint starts Chrome at boot            |
| `PROMPT_EXTRACTOR_VNC`         | `true`            | Enables Xvfb + noVNC on port 6080          |
| `VNC_SCREEN`                   | `1280x720x24`     | Virtual screen resolution                   |

### Key env vars (per-machine, set via `fly machine update`)

| Variable               | Example                   | Effect                                   |
|------------------------|---------------------------|------------------------------------------|
| `CHROME_PROFILE_INDEX` | `0`                       | Identifies which profile/account         |
| `CHATGPT_LOGIN_EMAIL`  | `dev@theround.com`        | Selects the account for this machine     |

---

## Login Process

Each machine's Chrome profile must be manually logged into ChatGPT once.
After that the session cookies persist indefinitely on the Fly volume.

### Initial login via VNC

1. Start the machine if stopped:
   ```bash
   fly machine start <machine-id> -a prompt-extractor-uk
   ```

2. Open the VNC URL in a browser. Because `auto_start_machines: false` is set
   and Fly load-balances across running machines, stop all other machines first
   so the public URL routes to the target:
   ```bash
   # Stop all others, leave only target running
   fly machine stop <other-id-1> <other-id-2> … -a prompt-extractor-uk
   ```
   Then open: `https://prompt-extractor-uk.fly.dev/vnc.html`

3. You'll see the Chrome window already open at `chatgpt.com`. Log in manually.

4. Once logged in, Chrome writes the session cookies to
   `/data/chrome-profile`. The session will be reused for all future runs on
   this machine.

5. Restart any other machines you stopped:
   ```bash
   fly machine start <other-id-1> … -a prompt-extractor-uk
   ```

### Session expiry

ChatGPT sessions can expire after extended periods or after being interrupted
by a Cloudflare challenge. Signs of expiry:
- Logs show `TimeoutError: Timed out waiting for ChatGPT prompt input`
- No Cloudflare signals detected in the logs

To recover: VNC into the affected machine and log back in. No code change or
restart is required — Chrome will pick up the new session immediately.

---

## Cloudflare "Are you human?" Challenge

Cloudflare occasionally intercepts ChatGPT sessions with a browser challenge
page. The extraction runner detects this and handles it gracefully.

### Detection

The runner checks for Cloudflare signals via JavaScript on the current page:
- Page title matches `/just a moment|are you human/i`
- CF DOM elements (`#challenge-running`, `.cf-browser-verification`, etc.)
- Turnstile iframe (`challenges.cloudflare.com`)
- Cloudflare scripts loaded on the page

### Behaviour

When a Cloudflare challenge is detected the runner **does not fail
immediately**. Instead it:
1. Logs a `WARNING` immediately identifying the challenge.
2. Logs a reminder every 30 seconds with elapsed/remaining time.
3. Continues waiting for the ChatGPT input to appear (i.e. for you to solve the
   challenge via VNC).
4. Resumes the extraction run normally once the challenge clears.
5. Only times out and fails if the full `login_wait_seconds` deadline is
   reached without the challenge being solved.

### Resolving via VNC

1. Watch the Prefect logs — you'll see warnings like:
   ```
   Cloudflare 'Are you human?' challenge detected during wait_for_login —
   VNC into this machine to resolve. title='Just a moment...' signals=['title_challenge', 'cf_element']
   ```
2. VNC into the affected machine (see [Login Process](#login-process)).
3. Complete the Cloudflare checkbox/challenge in the browser window.
4. The extraction run will resume automatically.

---

## Slack Notifications

The app sends automatic Slack alerts to **#dev** (`zeboraworkspace.slack.com`)
when operator intervention is required. No manual log-watching needed.

### Setup

The bot is configured via the Slack app manifest at `slack-app-manifest.json`
in the repo root. To install or re-install:

1. Go to https://api.slack.com/apps → **Create New App** → **From an app manifest**
2. Select workspace: `zeboraworkspace`, paste the JSON from `slack-app-manifest.json`
3. **OAuth & Permissions** → **Install to Workspace** → copy the Bot User OAuth Token (`xoxb-...`)
4. Set the token as a Fly secret:
   ```bash
   fly secrets set SLACK_BOT_TOKEN=xoxb-your-token-here -a prompt-extractor-uk
   ```
5. Apply to all machines (secrets stay staged until machines are individually updated):
   ```bash
   IMAGE="registry.fly.io/prompt-extractor-uk:deployment-<current-tag>"
   for id in e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 \
              7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678; do
     fly machine update $id -a prompt-extractor-uk --image $IMAGE --yes
   done
   bash scripts/apply_machine_envs.sh
   ```

> **Note on `fly secrets set` errors:** Due to the mixed-volume setup, `fly secrets set`
> and `fly secrets deploy` will always error on the volume mismatch. The secret is still
> stored — apply it by updating each machine individually as above, then verify with:
> ```bash
> fly ssh console -a prompt-extractor-uk --machine <machine-id> -C "printenv SLACK_BOT_TOKEN"
> ```

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | Yes (to enable) | — | Bot User OAuth Token (`xoxb-...`). If unset, notifications are silently skipped. |
| `SLACK_CHANNEL_ID` | No | `C0ABPV27S58` (#dev) | Slack channel to post alerts to. |

### Alert types

**Cloudflare challenge detected** — fired once when a challenge is first seen:

> ⚠️ **Cloudflare Challenge Detected**
> Machine ID: `e829491b6d4268` | Account: `dev@zebora.io`
> Region: `lhr` | Context: `wait_for_login`
> Page title: Just a moment... | Signals: `title_challenge`, `cf_element`
> URL: https://chatgpt.com/
> *Action required:* The run is paused. Open `https://prompt-extractor-uk.fly.dev/vnc.html` to VNC in and solve the challenge.

**Cloudflare challenge cleared** — fired when the challenge is resolved and the run resumes:

> ✅ Cloudflare challenge cleared on `e829491b6d4268` (`dev@zebora.io`) after 47s — run resuming.

### Testing notifications locally

```bash
source .venv/bin/activate
python3 - <<'EOF'
import os
os.environ["SLACK_BOT_TOKEN"] = "xoxb-your-token"
os.environ["SLACK_CHANNEL_ID"] = "C0ABPV27S58"
os.environ["FLY_MACHINE_ID"] = "test-machine"
os.environ["FLY_APP_NAME"] = "prompt-extractor-uk"
os.environ["FLY_REGION"] = "lhr"
os.environ["CHATGPT_LOGIN_EMAIL"] = "dev@theround.com"

from automated_extraction.notifications import notify_cloudflare_challenge, notify_cloudflare_cleared
notify_cloudflare_challenge(
    signals=["title_challenge", "cf_element"],
    title="Just a moment...",
    url="https://chatgpt.com/",
    context="wait_for_login",
)
import time; time.sleep(1)
notify_cloudflare_cleared(elapsed_seconds=47, context="wait_for_login")
EOF
```

### Implementation

Notifications are implemented in `automated_extraction/notifications.py`. The
module is a no-op if `SLACK_BOT_TOKEN` is not set, so local dev and test runs
are unaffected. All Slack calls are fire-and-forget — a failed notification
logs a warning but never causes an extraction run to fail.

The two trigger points in `chatgpt_runner.py`:

| Trigger point | When fired |
|---------------|-----------|
| `wait_for_login()` | CF challenge detected while waiting for the ChatGPT input to appear (e.g. on session start or after `--login-only`) |
| `_raise_if_cloudflare()` | CF challenge detected at the start of each individual prompt run |

---

## Prefect Integration

### Work pool

All UK workers share one Prefect work pool: `prompt-extraction-uk`.

Each machine runs one Prefect worker process:
```
python -m prefect worker start --pool prompt-extraction-uk --type process --limit 1 --install-policy never
```

`--limit 1` means each machine processes at most one flow run at a time
(matching the one-Chrome-per-machine constraint).

The work pool concurrency limit is set to 9 (one slot per machine) so all
machines can work simultaneously:
```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect work-pool set-concurrency-limit prompt-extraction-uk 9
```

### Flows

| Flow name | Deployment | Purpose |
|-----------|-----------|---------|
| `chatgpt-extraction` | `chatgpt-extraction-uk` | Single extraction run: load prompts, run ChatGPT, save outputs, trigger scoring |
| `chatgpt-extraction-batch` | `chatgpt-extraction-batch-uk` | Sequential batch runner: loops `chatgpt-extraction` in chunks until a batch is complete |
| `dispatch-extraction` | `dispatch-extraction-uk` | Dispatcher: counts remaining prompts, splits them across N workers, submits one batch run per worker |
| `google-ai-mode-extraction` | `google-ai-mode-extraction-uk` | Single Google AI Mode extraction run |
| `google-ai-mode-extraction-batch` | `google-ai-mode-extraction-batch-uk` | Sequential batch runner for Google AI Mode |
| `google-ai-overview-extraction` | `google-ai-overview-extraction-uk` | Single Google AI Overview extraction run |
| `google-ai-overview-extraction-batch` | `google-ai-overview-extraction-batch-uk` | Sequential batch runner for Google AI Overview |
| `prompt-output-processing` | `prompt-output-processing-uk` | Re-process saved outputs (markdown enrichment) without re-running ChatGPT |

### End-to-end extraction pipeline

For each prompt, `chatgpt-extraction` runs these steps in order:

```
1. Load prompts from API (filter: only_remaining, llm_model_filter=gpt)
2. For each prompt:
   a. Claim the prompt (atomic, prevents duplicate work across workers)
   b. Connect to persistent Chrome on localhost:9222
   c. Create a fresh ChatGPT chat
   d. Send the prompt, wait for the response
   e. Copy the response (markdown + raw HTML + sources)
   f. Capture product flyouts (if capture_products=true)
   g. Capture entity flyouts (if capture_entities=true)
   h. Save output to Supabase (prompts_outputs)
3. Post-process: product output task → entity output task → prompt output task
4. Trigger scoring API for each saved output
```

### Early-exit optimisation

When multiple workers are processing the same batch, a worker may finish its
chunk before its loop is complete (because other workers claimed the remaining
prompts). After any run where `saved_count=0` the batch runner queries the API
to check if any prompts remain. If none do, the loop exits immediately rather
than continuing through empty iterations.

This applies to all three batch runners (chatgpt, google-ai-mode,
google-ai-overview) and their mop-up passes.

---

## Running a Batch

### Trigger a single worker (test / targeted run)

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run \
  'chatgpt-extraction-batch/chatgpt-extraction-batch-uk' \
  --param batch_id=<batch-uuid> \
  --param limit=5 \
  --param login_email=dev@theround.com \
  --param capture_products=true \
  --param capture_entities=true
```

`login_email` pins the run to the machine with that account. Omit it to let
Prefect assign any available worker.

### Dispatch across all workers

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
prefect deployment run \
  'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<batch-uuid> \
  --param worker_count=9 \
  --param capture_products=true \
  --param capture_entities=true
```

The dispatcher:
1. Counts remaining prompts for the batch.
2. Divides them into `worker_count` equal chunks.
3. Submits one `chatgpt-extraction-batch-uk` run per chunk with a staggered
   startup delay so workers don't all hit ChatGPT simultaneously.

### Start / stop workers

```bash
# Start all machines
fly machine start e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 \
  7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678 \
  -a prompt-extractor-uk

# Stop all machines
fly machine stop e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 \
  7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678 \
  -a prompt-extractor-uk
```

---

## Deploying

### Standard deploy process

`fly deploy` builds a new Docker image and updates the primary machine. Because
the other 8 machines have custom volumes, the deploy command errors on them —
this is expected. After deploy, manually apply the new image to all machines:

```bash
# 1. Build and push the image (deploy will error on cloned machines — that's ok)
fly deploy -a prompt-extractor-uk -c fly-uk.yaml

# 2. Note the new image tag from the output, e.g.:
#    image: registry.fly.io/prompt-extractor-uk:deployment-01KS7KHXPKMNZNTND5TH1MJ8C3

# 3. Apply to all 9 machines
IMAGE="registry.fly.io/prompt-extractor-uk:deployment-<tag>"
for id in e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 \
           7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678; do
  fly machine update $id -a prompt-extractor-uk --image $IMAGE --yes
done

# 4. Re-apply per-machine env vars (fly machine update wipes them)
bash scripts/apply_machine_envs.sh
```

### Re-registering Prefect deployments

After code changes to flows, re-register them against the remote Prefect server:

```bash
source .venv/bin/activate
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
PREFECT_WORK_POOL=prompt-extraction-uk \
python -m automated_extraction.workflows.register_deployments --deploy-local --region uk
```

This must be run from the worktree that contains the code changes (i.e. the
branch you want deployed), not the main repo root if they differ.

### Why `apply_machine_envs.sh` must always be run after deploy

`fly machine update` (and `fly deploy`) wipes any env vars that were set
interactively via `fly machine update --env`. The per-machine vars
(`CHROME_PROFILE_INDEX`, `CHATGPT_LOGIN_EMAIL`) are not in `fly-uk.yaml`
because they differ per machine. `scripts/apply_machine_envs.sh` restores them
after every deploy.

---

## Monitoring & Debugging

### Prefect UI

```
https://prompt-extractor-prefect.fly.dev
```

Each flow run shows:
- Parameters (batch_id, login_email, limit, etc.)
- Logs from every extraction step
- Task-level state (completed / failed / timed-out)
- Final summary (saved_count, skipped_count, failed_count, product/entity counts)

### Live machine logs

```bash
fly logs -a prompt-extractor-uk --machine <machine-id>
```

### VNC into a machine

```bash
# Stop all other machines so the load balancer routes to the target
fly machine stop <other-ids…> -a prompt-extractor-uk

# Open in browser
open https://prompt-extractor-uk.fly.dev/vnc.html
```

### Check Chrome is running on a machine

```bash
fly ssh console -a prompt-extractor-uk --machine <machine-id> \
  -C "curl -s http://localhost:9222/json/version | python3 -m json.tool"
```

If Chrome is not running, check `/tmp/chrome-persistent.log`:

```bash
fly ssh console -a prompt-extractor-uk --machine <machine-id> \
  -C "tail -50 /tmp/chrome-persistent.log"
```

### Common issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Could not connect to persistent Chrome on port 9222` | Chrome crashed or not yet started | SSH in and check `/tmp/chrome-persistent.log`; the watchdog should restart it within ~10 s |
| `TimeoutError: Timed out waiting for ChatGPT prompt input` | Session expired or Cloudflare challenge | VNC in, log in or solve challenge |
| `[machine] already claimed by another worker` | Another machine took the prompt | Normal — the claiming system prevents duplicate work |
| All machines running but no flow runs picked up | Work pool concurrency limit too low | Run `prefect work-pool set-concurrency-limit prompt-extraction-uk 9` |
| Fly UI warning: "app running multiple images" | Some machines have an old image | Run the full deploy process above to unify all machines on one image |

---

## Adding a New Machine / Account

1. **Create the Fly machine** (clone an existing one or use `fly machine clone`).

2. **Attach a dedicated volume**:
   ```bash
   fly volume create prompt_extractor_data_<name> -a prompt-extractor-uk -r lhr --size 10
   fly machine update <new-machine-id> -a prompt-extractor-uk \
     --mount-point /data --volume <volume-id>
   ```

3. **Apply the current image**:
   ```bash
   fly machine update <new-machine-id> -a prompt-extractor-uk \
     --image registry.fly.io/prompt-extractor-uk:deployment-<current-tag> --yes
   ```

4. **Set per-machine env vars**:
   ```bash
   fly machine update <new-machine-id> -a prompt-extractor-uk \
     --env CHROME_PROFILE_INDEX=<next-index> \
     --env CHATGPT_LOGIN_EMAIL=<account@example.com> --yes
   ```

5. **Add to `scripts/apply_machine_envs.sh`** so env vars survive future deploys.

6. **Log in via VNC** (see [Login Process](#login-process)).

7. **Increase work pool concurrency**:
   ```bash
   PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
   prefect work-pool set-concurrency-limit prompt-extraction-uk <new-total>
   ```

8. **Test** with a 2-prompt batch run targeted at the new account:
   ```bash
   PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api \
   prefect deployment run 'chatgpt-extraction-batch/chatgpt-extraction-batch-uk' \
     --param batch_id=<batch-uuid> \
     --param limit=2 \
     --param login_email=<account@example.com> \
     --param capture_products=true \
     --param capture_entities=true
   ```
