# Monitor Mode

Use monitor mode when continuing an active extraction, replacing failed work, detecting stalls, pausing, resuming, or completing a batch.

## Required State

Parse these values from the prompt or saved state:

- `batch_id`
- `flow_runs`
- `machines`
- `worker_count`
- `extraction_type`
- `deployment_id`
- `app`
- `work_pool`
- `required_models` when set
- `prev_output_counts` for GPT-UK account zero-output detection
- `zero_output_accounts` for GPT-UK account rotation state
- `prev_model_counts` for delta reporting
- `consecutive_zero_replacements` for Google AI Overview cooldown handling

## Iteration Workflow

1. Query batch progress using `references/sql.md`.
2. For `gpt-uk`, query account health and check stale locks.
3. Reconcile active flow runs from the deployment.
4. Check worker heartbeats.
5. Decide whether to replace completed, failed, crashed, cancelled, or blocked flow runs.
6. For near-complete batches, scale down effective worker count before replacing flows.
7. Check completion.
8. Report progress, actions, and next state.
9. Schedule or print the next monitor prompt.

## Flow Reconciliation

Always query live deployment flow runs before trusting tracked IDs:

```bash
python .claude/skills/dispatch/scripts/prefect_api.py flow-runs \
  --deployment-id <deployment_id> \
  --tracked <id1,id2> \
  --limit 15
```

Rules:

- Keep live `RUNNING`, `SCHEDULED`, and `PENDING` flow runs.
- Drop tracked IDs that are `CANCELLED`, `COMPLETED`, `FAILED`, or `CRASHED`.
- Treat `SCHEDULED` flow runs older than 15 minutes as blocked worker slots.
- Treat `RUNNING` and `PENDING` as healthy unless progress indicates a stall.

## Worker Heartbeat

```bash
python .claude/skills/dispatch/scripts/prefect_api.py workers --work-pool <work_pool>
```

If heartbeat age is over 600 seconds and flows are stuck scheduled, restart the stale machine after confirmation.

## GPT-UK Stale Locks

Dry run first:

```bash
python .claude/skills/dispatch/scripts/supabase_locks.py stale --active-machines <m1,m2>
```

Release only after confirmation:

```bash
python .claude/skills/dispatch/scripts/supabase_locks.py stale --active-machines <m1,m2> --apply
```

Report released locks with a visible warning.

## GPT-UK Zero-Output Rotation

Compare current `last24h_total` for each `in_use` account to `prev_output_counts`.

- Increment the zero counter when the total is unchanged.
- Reset the zero counter when output increases.
- Rotate accounts with 2 or more consecutive zero-output iterations.

Rotation steps after confirmation:

1. Identify the account worker machine.
2. Cancel the flow running on that machine if known.
3. Release the profile lock.
4. Cycle the machine.
5. Dispatch a replacement flow.
6. Remove that account from `zero_output_accounts`.

## Replacement Rules

Replace immediately:

- `FAILED`
- `CRASHED`
- `CANCELLING`
- `CANCELLED`
- `SCHEDULED` for 15 minutes or more

Remove from tracking:

- `COMPLETED`

Dispatch a replacement for completed flow runs only if remaining work exists and active flow count is below the effective worker count.

Effective worker count:

```python
remaining = total_prompts - fully_complete
effective_workers = max(1, min(worker_count, math.ceil(remaining / limit)))
```

Use `limit = 25` for GPT-UK unless the original dispatch used a different value.

## Google AI Overview Auto-Cooldown

For `google-ai-overview`, track `consecutive_zero_replacements`.

Increment when:

- Delta is zero for all tracked models, and
- One or more replacement flows were dispatched this iteration.

Reset to zero when any model output increases.

If the counter reaches 2:

1. Stop all machines after confirmation unless the original monitor policy already authorized auto-cooldown.
2. Send a cooldown notification if host tooling exists.
3. Do not schedule another wakeup.
4. Print a resume prompt for manual continuation.

## Completion

Required-model batches complete when every prompt has every required model captured.

Non-required-model batches complete when no active flows remain and remaining count is unchanged for two checks.

On completion:

1. Stop all machines after confirmation or previously granted completion policy.
2. Report model counts, duplicate counts when available, and total time.
3. Notify if host tooling is available.
4. Do not schedule another monitor loop.

## Pause And Resume

When the user asks to pause mid-batch, save `.brandsight-resume.json` before stopping machines. Include:

```json
{
  "batch_id": "<batch_id>",
  "paused_at": "<iso_timestamp>",
  "fully_complete": 0,
  "total": 0,
  "model_counts": {},
  "machines": ["<m1>"],
  "extraction_type": "<type>",
  "deployment_id": "<deployment_id>",
  "app": "<app>",
  "work_pool": "<work_pool>",
  "required_models": ["<model1>"],
  "resume_prompt": "/dispatch --monitor ..."
}
```

Print the `resume_prompt`.

## Report Format

Use a concise status block:

```text
Iteration: <time UTC>
Batch: <batch_name> (<batch_id>)
Progress:
  <model>: <done>/<total> delta <+n>
Fully complete: <done>/<total>
Flow states: <id> RUNNING, <id> SCHEDULED blocked 18m
Actions: <none|released locks|cycled machines|created replacements>
Next check: 5 min
Resume prompt: /dispatch --monitor ...
```
