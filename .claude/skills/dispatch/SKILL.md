---
name: dispatch
description: Dispatch, monitor, pause, resume, and troubleshoot BrandSight prompt extraction jobs across GPT, Google AI Overview, Google AI Mode, Claude, and Perplexity using Prefect, Fly.io, and Supabase. Use when starting extraction workers, launching batch extraction, checking progress, rotating stuck accounts, releasing stale locks, replacing failed flow runs, stopping machines after completion, or producing a resume prompt for a paused extraction batch.
---

# BrandSight Extraction Dispatch

Use this skill to operate BrandSight prompt extraction safely from either Claude or Codex.

The original pre-portability implementation is backed up at `SKILL.original.md`.

## Operating Rules

- Treat machine starts/stops, lock releases, flow cancellations, and new dispatches as production actions. Confirm intent before running them unless the user explicitly requested that exact action.
- Prefer dry-run/status commands before mutating infrastructure.
- Use Bash-compatible commands and local scripts where possible. Do not assume host-specific tools such as `AskUserQuestion`, `ScheduleWakeup`, or `PushNotification` exist.
- If a host-specific tool is available, use the mapping in `references/platforms.md`. If not, use the fallback behavior there.
- Keep monitor state explicit in the prompt or a resume file so another agent can continue the run.

## Mode Selection

Parse the user's request or invocation arguments first.

- Wizard mode: start or dispatch a new extraction run. Read `references/wizard.md`.
- Monitor mode: arguments contain `--monitor`, flow run IDs, or a request to continue polling. Read `references/monitor.md`.
- Pause/resume mode: the user asks to pause, stop mid-batch, or resume a saved batch. Read `references/monitor.md`.
- Infrastructure/status mode: the user asks about workers, machine state, flow runs, deployments, or batch status. Read `references/infrastructure.md`.
- Platform compatibility questions: read `references/platforms.md`.

## Shared Inputs

Common extraction types:

- `gpt-uk`
- `gpt`
- `google-ai-overview`
- `google-ai-mode`
- `claude`
- `perplexity`

Monitor prompts should keep state inline:

```text
/dispatch --monitor batch_id=<batch_id> flow_runs=<id1,id2> machines=<m1,m2> worker_count=<n> extraction_type=<type> deployment_id=<deployment_id> app=<fly_app> work_pool=<pool> required_models=<model1,model2> prev_model_counts=<model:count>
```

Omit optional fields when empty. Include `required_models` for model-complete batches.

## Helper Scripts

Run helper scripts from the repository root.

- `scripts/prefect_api.py`: query Prefect deployments, flow runs, worker heartbeats, and optionally create flow runs.
- `scripts/build_monitor_prompt.py`: build a portable monitor prompt from explicit state.
- `scripts/supabase_locks.py`: inspect stale ChatGPT profile locks and release them only with `--apply`.
- `scripts/fly_machines.py`: list, start, stop, or cycle Fly machines; mutating actions require `--apply`.

These helpers are intentionally plain Bash/Python entrypoints so both Claude and Codex can use them through shell execution.

## Reference Map

- `references/platforms.md`: Claude and Codex compatibility behavior.
- `references/wizard.md`: interactive dispatch workflow and parameter selection.
- `references/monitor.md`: monitor loop, replacements, completion, pause, and resume.
- `references/infrastructure.md`: constants, deployments, Fly apps, and operational commands.
- `references/sql.md`: Supabase queries used by wizard and monitor modes.

Read only the references needed for the current request.
