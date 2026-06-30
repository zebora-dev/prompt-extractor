# Platform Compatibility

Use the same operational workflow in Claude and Codex. Only the interaction surface changes.

## Frontmatter

Keep the shared `SKILL.md` frontmatter to:

```yaml
---
name: dispatch
description: ...
---
```

Do not put host-specific fields such as `allowed-tools`, `argument-hint`, `disable-model-invocation`, or tool names in the shared frontmatter.

## Capability Mapping

| Need | Claude-style behavior | Codex-style fallback |
|---|---|---|
| Ask a structured question | `AskUserQuestion` when available | Ask a concise plain-text question, or use `request_user_input` when available |
| Schedule the next monitor check | `ScheduleWakeup` when available | Use an automation/reminder tool when available; otherwise print the exact resume prompt |
| Send completion notification | `PushNotification` when available | Report final summary in the thread; use notification/automation tooling only when available |
| Query Supabase | `mcp__supabase__execute_sql` if available | Use project scripts, `psql`, or `supabase-py` with environment variables |
| Write Supabase locks | Bash plus `supabase-py` | Bash plus `supabase-py`; never use read-only SQL tools for writes |
| Operate Fly machines | Bash with `flyctl` or `fly` | Bash with `flyctl` or `fly` |
| Query Prefect | Bash with Prefect CLI or HTTP API | Bash with Prefect CLI or HTTP API |

## Interaction Rules

- If structured-question tools are unavailable, ask one clear question at a time and continue with safe defaults when reasonable.
- If wakeups are unavailable, end monitor iterations with a copy-ready `/dispatch --monitor ...` prompt and the recommended delay.
- If notification tools are unavailable, include the final summary in the response.
- If a required credential or CLI is missing, stop before mutation and report the missing prerequisite.

## Confirmation Rules

Ask for explicit confirmation before:

- Starting or stopping Fly machines.
- Cancelling, replacing, or creating Prefect flow runs.
- Releasing Supabase locks.
- Triggering a new batch dispatch.
- Running a monitor action that will mutate infrastructure.

Status-only reads do not need confirmation.
