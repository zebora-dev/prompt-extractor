# Improvements & Roadmap

Tracked improvements for the prompt-extractor system. Items are grouped by theme and roughly ordered by priority within each group.

---

## 🐛 Bug Fixes / Quick Wins

### Chrome SingletonLock cleanup on startup
**Status:** Open  
**Effort:** 15 mins  
**Impact:** High — occurs on every machine restart after an abrupt stop

Chrome fails to start when a machine is restarted mid-run because a stale `SingletonLock` file is left on the Fly volume. Currently requires manual SSH to clear.

**Fix:** Add to `docker/entrypoint.sh` before Chrome starts:
```bash
rm -f /data/chrome-profile/SingletonLock \
       /data/chrome-profile/SingletonSocket \
       /data/chrome-profile/SingletonCookie
```

---

### Prompt claims — reduce claim window / auto-expire stale claims
**Status:** Open  
**Effort:** Half day  
**Impact:** High — causes missed prompts at end of batch runs

When a flow fails mid-run (stale Chrome profile, Cloudflare challenge, crash), `prompt_claims` records remain claimed but unprocessed. This causes the batch to appear near-complete while a small number of prompts are silently stuck — sometimes never processed.

**Fix:**
- Reduce the claim TTL window (e.g. from current value to ~10–15 mins)
- Add a background Prefect task / scheduled flow that finds claims older than TTL and releases them back to the queue
- Add a mop-up pass at the end of each batch run that explicitly reclaims and retries any abandoned prompts

---

### Prefect server 503s on bulk dispatch
**Status:** Open  
**Effort:** 1 hour  
**Impact:** Medium — requires manual retry steps after every large dispatch

The Prefect server (single `2048mb` machine) returns 503 errors when dispatching 10–15 flows in quick succession. Currently requires manual identification and re-submission of failed flows.

**Fix (either/both):**
- Add exponential backoff retry logic inside `submit_batch_workers()` so retries are automatic
- Upgrade Prefect server VM to `4096mb`

---

## 🔔 Notifications & Observability

### VNC direct machine link in Cloudflare Slack alert
**Status:** Open  
**Effort:** Half day  
**Impact:** High — currently takes multiple attempts to VNC into the right worker

When a Cloudflare turnstile challenge is detected, we receive a Slack alert with the VNC URL (`https://prompt-extractor-uk.fly.dev/vnc.html`). However the Fly load balancer routes to a random running machine, so it's hit-or-miss whether you land on the affected worker. This can waste significant time.

**Fix:**
- The notification already has `FLY_MACHINE_ID` — use this to construct a direct machine-specific VNC URL
- Fly private networking allows direct machine access via `<machine-id>.vm.<app-name>.internal` — surface this in the Slack message
- Alternative: stop all other machines temporarily so the LB routes directly to the affected one (could be automated in the alert handler)

---

### Slack notification on batch completion
**Status:** Open  
**Effort:** Half day  
**Impact:** High — currently have to manually poll batch status to know when to shut down workers

When all prompts in a batch have been processed (remaining = 0), there is no automatic notification. Workers continue running and consuming compute unnecessarily until manually stopped.

**Fix:**
- Add a completion check at the end of each `chatgpt-extraction-batch` / `google-ai-overview-extraction-batch` run
- If `remaining == 0` across all models for the batch, send a Slack message:  
  > ✅ Batch `{batch_id}` complete — all {N} prompts extracted. Workers can be shut down.
- Optionally auto-stop workers on completion (with a config flag)

---

### Chrome health check — silent crash detection
**Status:** Open  
**Effort:** Half day  
**Impact:** Medium — Chrome crashes are currently silent until manually spotted

Workers can appear `RUNNING` in Prefect while Chrome has crashed inside the machine. No alert is fired; the flow will eventually time out but this can waste a full run window.

**Fix:** Scheduled Prefect flow that polls `localhost:9222/json/version` on each machine every 5 minutes and sends a Slack alert if Chrome is unreachable, including machine ID and account email.

---

## 🤖 Automation

### Extraction Agent — autonomous Slack-driven operator
**Status:** Planned (see `abundant-forging-phoenix.md` plan)  
**Effort:** 1–2 weeks  
**Impact:** Very high — eliminates all manual worker/dispatch management

Currently every operation (start workers, set concurrency, dispatch flows, monitor progress, stop workers) requires manual intervention via this chat. The extraction agent would handle the full lifecycle from a single Slack command.

**Stack:** LangGraph + Claude Sonnet + Neon Postgres checkpointing + slack-bolt Socket Mode + Langfuse observability  
**Trigger:** `"run batch X on 9 UK workers with google-ai-overview"`  
**Behaviour:** Spin up workers → dispatch → monitor → auto re-dispatch on stall → stop workers → notify complete

See `abundant-forging-phoenix.md` for the full architecture plan.

---

### Stall detection & auto re-dispatch
**Status:** Open  
**Effort:** Half day (as standalone) / included in extraction agent  
**Impact:** High — stalls currently go undetected until manually spotted

When workers are online but no new outputs are appearing (e.g. all flows completed but prompts remain), the batch stalls silently. Currently caught by manual status checks.

**Fix (standalone):** Scheduled Prefect flow that:
1. Polls output count every 5 minutes
2. If `outputs_count` hasn't increased across 2 consecutive checks → submits 5 additional flows + notifies Slack
3. Resets counter after re-dispatch

---

### `apply_machine_envs.sh` — auto-run after deploy
**Status:** Open  
**Effort:** 30 mins  
**Impact:** Low-medium — easy to forget, causes misconfigured machines

`fly machine update` wipes per-machine env vars (`CHATGPT_LOGIN_EMAIL`, `CHROME_PROFILE_INDEX`) on every image update. `apply_machine_envs.sh` must be re-run manually after every deploy.

**Fix:** Wrap the full deploy process in a single `scripts/deploy.sh` that chains image update + env re-apply, so it's impossible to forget.

---

## 🆕 New Extraction Types

### Claude extraction process
**Status:** Open  
**Effort:** TBD  
**Impact:** High — opens up Claude.ai as a data source alongside ChatGPT and Google

Build a Claude extraction runner mirroring the existing ChatGPT flow:
- Persistent browser session logged into Claude.ai
- Same prompt dispatch / claiming / output saving pipeline
- New Prefect deployments: `claude-extraction`, `claude-extraction-batch`, `claude-extraction-batch-uk`, `claude-extraction-batch-us`
- New `llm_model` filter value (e.g. `claude-sonnet`)
- Considerations: Claude.ai rate limits, session handling, response format differences vs ChatGPT

---

