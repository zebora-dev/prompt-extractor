# Dispatch Skill

The BrandSight dispatch skill lives at:

```text
.claude/skills/dispatch/
```

It is written to be usable from both Claude and Codex. Claude can use the repository-local
`.claude/skills` location directly. Codex can use it manually by path, or automatically after
installing/symlinking the skill into the Codex skills directory.

## Layout

```text
.claude/skills/dispatch/
|-- SKILL.md              # Portable entrypoint; strict name/description frontmatter only
|-- SKILL.original.md     # Backup of the original monolithic Claude-oriented skill
|-- references/
|   |-- infrastructure.md # Fly, Prefect, deployment, and status commands
|   |-- monitor.md        # Monitor loop, replacements, pause/resume, completion
|   |-- platforms.md      # Claude/Codex capability mapping and fallbacks
|   |-- sql.md            # Supabase read queries
|   `-- wizard.md         # New-dispatch workflow and parameter selection
`-- scripts/
    |-- build_monitor_prompt.py
    |-- fly_machines.py
    |-- prefect_api.py
    `-- supabase_locks.py
```

`SKILL.md` stays small so both models can load it cheaply. Detailed procedures live in
`references/`, and repeatable operational commands live in `scripts/`.

## Safety Model

Status-only commands can run without confirmation. Production-changing actions require explicit
operator confirmation unless the user already requested the exact action:

- starting or stopping Fly machines
- cancelling, replacing, or creating Prefect flow runs
- releasing Supabase locks
- triggering a new batch dispatch
- running monitor actions that mutate infrastructure

Helper scripts follow the same rule. For example, `fly_machines.py` and `supabase_locks.py`
default to dry-run behavior and require `--apply` for mutation.

## Triggering In Claude

Claude should discover the skill from `.claude/skills/dispatch`.

Examples:

```text
/dispatch gpt-uk <batch_id>
/dispatch --monitor batch_id=<batch_id> flow_runs=<ids> machines=<ids> worker_count=3 extraction_type=gpt-uk deployment_id=<id> app=gpt-extractor-uk work_pool=gpt-extraction-uk
```

Claude-specific tools such as `AskUserQuestion`, `ScheduleWakeup`, and `PushNotification` are
not required by the portable skill. If Claude provides them, follow `references/platforms.md`.

## Triggering In Codex

Codex does not automatically discover arbitrary repository `.claude/skills` folders in every
environment. Use one of these approaches.

### Option A: Manual Path Invocation

Ask Codex to use the skill by path:

```text
Use the dispatch skill at .claude/skills/dispatch to check worker status for gpt-extractor-uk.
Do not mutate infrastructure.
```

For a new dispatch:

```text
Use the dispatch skill at .claude/skills/dispatch to prepare a gpt-uk dispatch for batch <batch_id>.
Show me the confirmation summary before starting machines or creating flow runs.
```

For monitor mode:

```text
Use the dispatch skill at .claude/skills/dispatch to run one monitor iteration:
/dispatch --monitor batch_id=<batch_id> flow_runs=<ids> machines=<ids> worker_count=<n> extraction_type=<type> deployment_id=<id> app=<app> work_pool=<pool>
Do not apply mutations without asking first.
```

### Option B: Install For Automatic Codex Triggering

Symlink the repository skill into the Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/.claude/skills/dispatch" "${CODEX_HOME:-$HOME/.codex}/skills/dispatch"
```

If a `dispatch` skill already exists there, remove it or choose a different target after checking
that it is not needed.

After that, start a fresh Codex session so the skill list refreshes. You can then trigger it with:

```text
Use $dispatch to check batch <batch_id> status.
```

or:

```text
Use the dispatch skill to prepare a Google AI Overview UK dispatch for batch <batch_id>.
```

## Useful Dry Runs

Build a monitor prompt:

```bash
python .claude/skills/dispatch/scripts/build_monitor_prompt.py \
  --batch-id <batch_id> \
  --flow-runs <id1,id2> \
  --machines <m1,m2> \
  --worker-count 2 \
  --extraction-type gpt-uk \
  --deployment-id <deployment_id> \
  --app gpt-extractor-uk \
  --work-pool gpt-extraction-uk \
  --required-models gpt-5-5,gpt-5-3-mini
```

List Fly machines:

```bash
python .claude/skills/dispatch/scripts/fly_machines.py list --app gpt-extractor-uk
```

Preview a machine start without executing it:

```bash
python .claude/skills/dispatch/scripts/fly_machines.py start --app gpt-extractor-uk --machines <machine_id>
```

Check stale locks without releasing them:

```bash
python .claude/skills/dispatch/scripts/supabase_locks.py stale --active-machines <m1,m2>
```

## Validation

Validate the skill metadata with Codex's skill creator validator when available:

```bash
python /path/to/skill-creator/scripts/quick_validate.py .claude/skills/dispatch
```

The portable skill should validate with only `name` and `description` in frontmatter.
