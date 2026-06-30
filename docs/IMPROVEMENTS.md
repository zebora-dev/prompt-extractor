# Improvements & Roadmap

Tracked improvements for the prompt-extractor system. Items are grouped by theme and roughly ordered by priority within each group.

---

## ✅ Recently Fixed

### Google AI Overview — CAPTCHA wait, no-output watchdog, auto-cooldown, timing jitter
**Fixed:** 2026-06-30  
**Files:** `automated_extraction/google_ai_overview_runner.py`, `automated_extraction/extraction.py`, `automated_extraction/notifications.py`, `.claude/skills/dispatch/SKILL.md`  
**Details:** [docs/GOOGLE_AI_OVERVIEW_IMPROVEMENTS.md](GOOGLE_AI_OVERVIEW_IMPROVEMENTS.md)

Following a 75-min silent stall during a live Range Rover batch run (both workers showed `RUNNING` but produced zero DB output), four improvements were added:

1. **CAPTCHA Slack notify + VNC wait** — `detect_blocking_page()` now sends a Slack alert with a VNC deep-link and waits up to 10 min for the operator to solve it, then resumes automatically (mirrors GPT Cloudflare handling).
2. **No-output watchdog** — after 10 consecutive non-triggered saves AND 15 min since last real AI Overview, the extraction job raises a sentinel error and exits cleanly so the monitor can restart.
3. **Monitor auto-cooldown** — after 2 consecutive replacement cycles with Δ=0, the monitor stops all machines, sends a `❄️ cooldown` Slack alert, and ends the loop. Operator resumes manually once Google's block decays.
4. **Timing jitter** — inter-prompt delay widened to 5–15s; added 2–6s pre-search jitter inside `run_prompt` to reduce detection fingerprint.

---

### `completed_prompt_ids` / `_active_claimed_ids` — Supabase 1000-row limit
**Fixed:** 2026-06-15  
**Files:** `automated_extraction/supabase_prompt_outputs.py`

Supabase's default page size is 1000 rows. For batches where `gpt-5-3-mini` had 1017+ output rows, `completed_prompt_ids()` silently truncated the result — missing prompt IDs that were actually complete. Workers loaded those prompts, found them already done on the per-prompt check, and skipped everything. Net result: no new outputs on any flow run despite 175 prompts remaining.

Same bug affected `_active_claimed_ids`, allowing workers to pick up already-claimed prompts when claims exceeded 1000 rows.

**Fix:** Added `.limit(10000)` to the per-model queries in both `completed_prompt_ids()` and `_active_claimed_ids()`.

---

### Duplicate `active=true` rows per prompt+model
**Fixed:** 2026-06-15  
**Files:** `automated_extraction/supabase_prompt_outputs.py`, `automated_extraction/extraction.py`

Two bugs caused multiple `active=true` rows to accumulate for the same `prompt_id + batch_id + llm_model`:

**Bug 1 — Concurrent check used `required_models` instead of exact captured model:**  
When `required_models = ["gpt-5-5", "gpt-5-3-mini"]` and a prompt had `gpt-5-3-mini` but not `gpt-5-5`, the pre-save concurrent check returned `None` (not complete) — allowing a worker to save a new `gpt-5-5` row even if one already existed. This happened on every re-dispatch, producing up to 13 active `gpt-5-5` rows per prompt.

**Fix:** Concurrent check now uses `llm_model_filter=capture.llm_model` (the exact model just captured) instead of `required_models`. Only blocks the save if *this specific model* already has an output.

**Bug 2 — No deactivation on save:**  
`save_prompt_output()` did a plain `.insert()` with no deactivation of previous rows. Even if two workers raced past the concurrent check simultaneously, both rows survived.

**Fix:** `save_prompt_output()` now runs an `.update({"active": False})` on any existing active rows for the same `prompt_id + batch_id + llm_model` before inserting. Guarantees exactly one active row per prompt+model regardless of concurrency.

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

### Prompt claims — claim per specific model when `required_models` is set
**Status:** Open  
**Effort:** Half day  
**Impact:** Medium — prevents wasted Chrome sessions re-running prompts for a model already captured

When `required_models` is set, claims are registered with the broad filter string `"gpt"` rather than the specific model being targeted (e.g. `"gpt-5-5"`). After a worker saves `gpt-5-5` and deletes its claim, the prompt re-enters the remaining set (still needs `gpt-5-3-mini`). Another worker picks it up, claims it for `"gpt"` again, Chrome runs, ChatGPT uses `gpt-5-5` again — wasting a full Chrome session. The deactivation-on-save fix prevents bad data, but the wasted session remains.

**Fix:** When `required_models` is set, identify which specific model is missing for each prompt before claiming, and register the claim with that model name. This requires a per-prompt model gap check before the claim step.

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

