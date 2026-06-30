# Dispatch Monitor — Reliability Fixes & Optimisations

This document describes the 10 improvements made to the gpt-uk extraction dispatch loop.
The current portable dispatch skill is documented in `docs/DISPATCH_SKILL.md`; monitor
procedures now live in `.claude/skills/dispatch/references/monitor.md`, with shared helper
scripts in `.claude/skills/dispatch/scripts/`. The original monolithic skill is preserved at
`.claude/skills/dispatch/SKILL.original.md`.

Related implementation files include `extraction.py`, `profile_manager.py`, and
`migrations/003_profile_quality_aware_claiming.sql`.

---

## Background

During a live Range Rover Jun-2026 gpt-uk batch run, four recurring problems were observed
that motivated the first set of fixes, followed by six further optimisations to improve
throughput, intelligence, and automation.

---

## Reliability Fixes (4)

### Fix 1 — Machine stop/start before redispatch

**Problem:** Cancelling a Prefect flow does NOT kill the Python process on the Fly machine.
Old processes held the Prefect worker slot, so new SCHEDULED flows never transitioned to
RUNNING.

**Fix:** Before dispatching a replacement flow, the monitor now stops and restarts the machine:
```bash
flyctl machines stop <machine_id> -a <app>
sleep 5
flyctl machines start <machine_id> -a <app>
sleep 15  # allow Prefect worker to reconnect
```

Also applies when a flow has been SCHEDULED for >15 minutes without transitioning to RUNNING
(treated as a blocked worker slot).

---

### Fix 2 — Zero-output worker auto-rotation

**Problem:** A RUNNING flow with 0 outputs is invisible to the basic flow state check. A
dead-but-running worker held an account locked indefinitely.

**Fix:** The monitor tracks `prev_output_counts=index:total` across iterations. If an in-use
account shows no new outputs for 2 consecutive iterations, it auto-rotates:
1. Cancel the flow
2. Release the profile lock via supabase-py
3. Stop/start the machine
4. Dispatch a replacement

Zero-iteration counters are tracked in the ScheduleWakeup prompt as `zero_output_accounts=index:count`.

---

### Fix 3 — Stale lock auto-detection & release

**Problem:** Accounts locked by machines no longer running flows stayed locked indefinitely.

**Fix:** Each monitor iteration cross-references `locked_by` against the known active machine
list (`machines=` arg). Any account locked by a machine NOT in the list is auto-released:
```python
from supabase import create_client
client.table('chatgpt_profiles').update({
    'is_locked': False, 'locked_by': None,
    'locked_at': None, 'lock_expires_at': None
}).eq('index', stale_index).execute()
```

---

### Fix 4 — Flow reconciliation by deployment

**Problem:** The monitor tracked a fixed list of flow run IDs. After rotations, old IDs became
phantoms. New flows dispatched outside the loop were never picked up.

**Fix:** At the start of each iteration, the monitor queries the deployment's recent flow runs
and reconciles against tracked IDs — dropping phantoms, adding newly discovered live flows.
The `flow_runs=` arg in ScheduleWakeup is always rebuilt from the reconciled live set.

---

## Optimisations (6)

### Opt 1 — Δ (delta) progress tracking

**Problem:** The monitor showed absolute counts but not velocity. A stalled batch looked the
same as a fast-moving one.

**Fix:** `prev_model_counts=model:done` is carried in the ScheduleWakeup prompt. Each
iteration computes Δ and shows it in the report:

```
Model          Done    Δ    Total   Dupes
gpt-5-5         345   +12   345     0
gpt-5-3-mini    648    +9   648     3
Fully complete:  218 / 759   Δ+10
```

`⚠ STALLED` is shown if any required model has Δ=0. Escalates to `🚨 STALLED 2 ITERATIONS`
if it persists.

---

### Opt 2 — Quality-aware profile claiming

**Problem:** `acquire_chatgpt_profile` ordered purely by LRU. A rested account with a 13%
gpt-5-5 rate was claimed before a similarly-rested one with 100%.

**Fix:** New `gpt55_lifetime_count` and `total_lifetime_count` columns on `chatgpt_profiles`.
New `acquire_chatgpt_profile_quality` RPC orders by success rate (new accounts default to
neutral 0.5 so they're tried before known-low accounts). New `update_profile_session_stats`
RPC is called by `extraction.py` at session end to keep counters current.

Migration: `migrations/003_profile_quality_aware_claiming.sql`
Python: `profile_manager.acquire_profile_quality()`, `profile_manager.update_session_stats()`

---

### Opt 3 — Lock TTL reduced from 4h → 1.5h

**Problem:** A crashed machine left its account locked for up to 4 hours.

**Fix:** Default TTL for `acquire_chatgpt_profile` and `refresh_chatgpt_profile_lock` reduced
to 1.5 hours. Locks are refreshed every ~30 min during active use, providing 3× headroom
without ever expiring live sessions. Orphaned locks from crashes release far sooner.

`_LOCK_HOURS_DEFAULT` in `profile_manager.py` updated accordingly.

---

### Opt 4 — Rolling rate downgrade detection

**Problem:** The consecutive-downgrade counter resets on any single gpt-5-5 response. An
account alternating gpt-5-5/mini/gpt-5-5/mini never triggered rotation.

**Fix:** A 10-prompt circular buffer tracks gpt-5-5 hit rate per session. If the rolling rate
drops below 40% after ≥10 prompts, rotation triggers with `reason="rate_degraded"`.

This works alongside the existing consecutive counter — either path triggers rotation.

---

### Opt 5 — Resume state on explicit stop

**Problem:** When the user stopped the monitor mid-batch, there was no record of where it was.
Resuming required reconstructing batch_id, machines, progress, and account state from memory.

**Fix:** When the user explicitly stops the monitor, it writes `.brandsight-resume.json` to
the project root before stopping machines. The file contains the full `resume_prompt` string
that can be pasted directly to restart monitoring.

On the next `/dispatch` invocation, if `.brandsight-resume.json` exists, the wizard offers to
resume from it.

---

### Opt 6 — Dynamic worker count scale-down

**Problem:** Near batch completion, 3 workers raced for the last few prompts — 2 were idle
while all 3 held accounts locked.

**Fix:** Each monitor iteration computes:
```python
effective_workers = max(1, min(worker_count, math.ceil(remaining / limit)))
```

When `effective_workers < worker_count`, completed flows are not replaced — the active count
naturally drains to the effective target. The report shows: `Scaling 3→1 workers (22 prompts remaining)`.

---

## Files Changed

| File | Change |
|---|---|
| `.claude/skills/dispatch/SKILL.md` | Portable skill entrypoint and routing |
| `.claude/skills/dispatch/references/monitor.md` | Monitor loop logic for the reliability improvements |
| `.claude/skills/dispatch/scripts/` | Portable Prefect, Fly, Supabase, and monitor-prompt helpers |
| `.claude/skills/dispatch/SKILL.original.md` | Backup of the original monolithic skill |
| `automated_extraction/extraction.py` | Opt 2 session stats reporting; Opt 4 rolling rate detection |
| `automated_extraction/profile_manager.py` | Opt 2 `acquire_profile_quality()`, `update_session_stats()`; Opt 3 TTL default |
| `migrations/003_profile_quality_aware_claiming.sql` | Opt 2 new columns + RPCs; Opt 3 TTL defaults |
