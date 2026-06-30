# Google AI Overview — Reliability Improvements

This document describes the improvements made to the Google AI Overview extraction pipeline
following a live incident during a Range Rover Jun-2026 batch run where both workers silently
stalled for 75 minutes with Prefect still showing `RUNNING`.

---

## Root-Cause Analysis

The original extraction flow had three compounding failure modes:

1. **Silent Google block (no exception thrown):** When Google blocked searches with a "no AI
   Overview" response (not a CAPTCHA URL), `run_prompt` saved an empty output (`ai_overview_triggered=False`).
   This counted as `saved_count += 1`, so `consecutive_all_failed` never fired — the flow ran
   indefinitely saving empty rows.

2. **CAPTCHA raised immediately, no human resolution path:** `detect_blocking_page()` raised
   `RuntimeError` on first CAPTCHA hit. With two workers hitting Google simultaneously, both
   crashed and the batch stalled until the monitor noticed (up to 75 min later).

3. **Monitor detected stalls too slowly:** The monitor compared Δ counts across 5-min intervals.
   A stall had to persist for a full polling cycle before it was detected, and detection relied
   on Δ=0 which requires two consecutive checks — meaning a stall could go unnoticed for 10+
   minutes beyond when it started.

---

## Improvements

### 1. CAPTCHA detection → Slack notification + VNC wait

**File:** `automated_extraction/google_ai_overview_runner.py`

When `detect_blocking_page()` finds a CAPTCHA or unusual-traffic page, the runner no longer
raises immediately. Instead it:

1. Sends a Slack notification with a VNC deep-link to the blocked machine so an operator can
   log in and solve it
2. Polls every 5 seconds for up to 10 minutes for the page to clear
3. Sends a "cleared" Slack notification and resumes extraction when resolved
4. Raises `RuntimeError` (causing a clean flow exit) if the 10-minute deadline expires

This mirrors the existing Cloudflare handling in the ChatGPT runner (`wait_for_login()`).

```python
# google_ai_overview_runner.py — new method on GoogleAIOverviewRunner
def wait_for_captcha_clear(
    self,
    *,
    context: str,
    batch_id: str | None = None,
    wait_seconds: int = 600,  # 10 minutes
) -> None:
    ...
```

The VNC URL format is:
```
https://<app>.fly.dev/vnc/<machine_id>/vnc.html?autoconnect=true&path=vnc/<machine_id>/websockify
```

#### New Slack notifications (notifications.py)

| Function | When fired |
|---|---|
| `notify_google_captcha()` | First CAPTCHA/block detection on a machine |
| `notify_google_captcha_cleared()` | CAPTCHA resolved, run resuming |
| `notify_google_cooldown()` | Auto-shutdown triggered by monitor (see below) |

---

### 2. No-output watchdog in extraction loop

**File:** `automated_extraction/extraction.py`

The extraction loop now tracks two counters:
- `last_triggered_at` — timestamp of the last save where `ai_overview_triggered=True`
- `consecutive_no_trigger` — number of consecutive saves with no AI Overview

After each non-triggered save:
```python
if (
    last_triggered_at is not None
    and consecutive_no_trigger >= 10       # 10 consecutive misses
    and time.time() - last_triggered_at > 900  # 15 min since last real result
):
    raise RuntimeError("no_overview_watchdog: ...")
```

The watchdog error re-raises (bypasses the per-prompt `except` handler) so it propagates out
of the extraction job, crashes the Prefect flow, and the monitor can detect the FAILED/CRASHED
state and take action.

This catches the case where Google returns "real-looking" search results pages but silently
omits AI Overviews — a state that was previously invisible to the pipeline.

---

### 3. Monitor-level auto-cooldown

**File:** `.claude/skills/dispatch/SKILL.md`

The dispatch monitor tracks a new `consecutive_zero_replacements` counter across iterations.
It increments when:
- All model Δ counts are 0 this iteration, AND
- At least one replacement flow was dispatched this iteration (flows failed and were replaced)

When the counter reaches 2:
1. All machines are stopped (`flyctl machines stop`)
2. `notify_google_cooldown()` sends a Slack alert with batch details and machine list
3. The monitor loop ends — no further ScheduleWakeup is scheduled

This implements the "20 minutes of inactivity + multiple retries → shut down and cool down"
requirement. The operator resumes manually with `/dispatch --monitor ...` once Google's block
clears (typically after 30–60 minutes).

**ScheduleWakeup prompt parameter:** `consecutive_zero_replacements=<N>` (omit when 0).

---

### 4. Faster stall detection via last-output age query

**File:** `.claude/skills/dispatch/SKILL.md`

For `google-ai-overview` batches, each monitor iteration now runs an additional query:

```sql
SELECT
  EXTRACT(EPOCH FROM (NOW() - MAX(run_at))) / 60 AS minutes_since_last_output
FROM prompts_outputs
WHERE batch_id = '<batch_id>' AND active = true AND llm_model = 'google-ai-overview';
```

If `minutes_since_last_output > 20` while flows are in `RUNNING` state, the monitor displays:

```
⚠ STALL DETECTED: no new outputs in <N> min (flows still RUNNING)
```

This detects stalls within the current polling window rather than needing two consecutive Δ=0
checks — reducing detection time from potentially 10+ minutes to ~5 minutes.

---

### 5. Randomised inter-search timing

**Files:** `automated_extraction/extraction.py`, `automated_extraction/google_ai_overview_runner.py`

Two timing changes reduce Google's bot-detection fingerprint:

| Location | Old | New |
|---|---|---|
| Between prompts (extraction.py) | `random.uniform(3.0, 7.0)` | `random.uniform(5.0, 15.0)` |
| Pre-search jitter (run_prompt) | none | `random.uniform(2.0, 6.0)` |

The pre-search jitter happens inside `run_prompt` before each `search_via_box` call. Combined
with the existing character-by-character typing and overlay-dismissal delays in
`google_chrome_factory.py`, each search now has a total pre-result delay of ~12–25 seconds
with significant variance between runs.

---

## Files Changed

| File | Change |
|---|---|
| `automated_extraction/notifications.py` | New: `notify_google_captcha`, `notify_google_captcha_cleared`, `notify_google_cooldown` |
| `automated_extraction/google_ai_overview_runner.py` | New: `wait_for_captcha_clear()` method; CAPTCHA handling in `run_prompt` and `wait_for_ai_overview`; timing jitter; `import random` |
| `automated_extraction/extraction.py` | No-output watchdog counters + re-raise logic; wider inter-prompt delay |
| `.claude/skills/dispatch/SKILL.md` | Auto-cooldown logic; last-output-age stall query; `consecutive_zero_replacements` param |

---

## Operational runbook

### CAPTCHA fired on a machine

1. Watch for Slack alert: `🤖 Google CAPTCHA on machine <id>` with a VNC link
2. Click the VNC link (or open `https://<app>.fly.dev/vnc/<machine_id>/vnc.html?autoconnect=true&...`)
3. Solve the CAPTCHA in the browser — the runner polls every 5s and will resume automatically
4. Slack sends `✅ Google CAPTCHA cleared on <machine_id>` when the run resumes
5. If you can't solve it within 10 min, the flow exits cleanly and the monitor dispatches a replacement

### Auto-cooldown triggered

1. Slack sends `❄️ Google extraction auto-shutdown` — all machines stopped
2. Wait 30–60 minutes for Google's IP block to decay
3. Start machines manually: `flyctl machines start <id> -a <app>`
4. Resume monitoring: `/dispatch --monitor batch_id=<id> flow_runs=... machines=... ...`

### Checking for stall manually

```sql
SELECT
  EXTRACT(EPOCH FROM (NOW() - MAX(run_at))) / 60 AS minutes_since_last_output,
  COUNT(DISTINCT prompt_id) AS total_done
FROM prompts_outputs
WHERE batch_id = '<batch_id>' AND active = true AND llm_model = 'google-ai-overview';
```

If `minutes_since_last_output > 20` and flows are RUNNING → likely stalled. Check VNC for CAPTCHA
or restart machines and redispatch.
