# Wizard Mode

Use wizard mode when starting a new extraction dispatch or preparing one for confirmation.

## Inputs

Parse any prefilled values from the user request:

- Extraction type: `gpt-uk`, `gpt`, `google-ai-overview`, `google-ai-mode`, `claude`, or `perplexity`.
- Batch ID: UUID.
- Worker count.
- Region for Google types: `uk` or `us`.

If a required value is missing, ask the user. In Codex, ask a concise plain-text question unless a structured input tool is available.

## Workflow

1. Determine extraction type and region.
2. Look up batch metadata and required models using `references/sql.md`.
3. Show current progress before dispatching.
4. Resolve Fly app, work pool, and Prefect deployment from `references/infrastructure.md`.
5. Show machine state and, for `gpt-uk`, account pool health.
6. Choose worker count. For `gpt-uk`, do not exceed non-disabled, non-cooling profiles.
7. Choose run parameters.
8. Present a confirmation summary.
9. After explicit confirmation, start needed machines and dispatch flow runs.
10. Build the monitor prompt and either schedule it with host tooling or print it with a recommended 5 minute delay.

## Parameter Defaults

`gpt-uk`:

```json
{
  "model_filter": "gpt",
  "limit": 25,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "capture_products": false,
  "capture_entities": false
}
```

Ask whether to capture products and entities. If no selection is made, both default to false.

`gpt`:

```json
{
  "model_filter": "gpt",
  "limit": 25,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "capture_products": false,
  "capture_entities": false
}
```

Google types:

```json
{
  "model_filter": "google-ai-overview",
  "limit": 5,
  "delay_seconds": 60,
  "trigger_scoring": true,
  "use_proxy": false
}
```

For `google-ai-mode`, set `model_filter` to `google-ai-mode`.

Claude:

```json
{
  "model_filter": "claude",
  "limit": 5,
  "delay_seconds": 120,
  "trigger_scoring": true
}
```

Perplexity:

```json
{
  "model_filter": "perplexity",
  "limit": 5,
  "delay_seconds": 120,
  "trigger_scoring": true
}
```

Add `batch_id` and `startup_delay_seconds` per worker. Stagger workers by 15 seconds.

## Dispatch

Use `prefect_api.py create-flow-run` after confirmation:

```bash
python .claude/skills/dispatch/scripts/prefect_api.py create-flow-run \
  --deployment-id <deployment_id> \
  --params-json '<json_params>'
```

Collect all flow run IDs.

Build the monitor prompt:

```bash
python .claude/skills/dispatch/scripts/build_monitor_prompt.py \
  --batch-id <batch_id> \
  --flow-runs <id1,id2> \
  --machines <m1,m2> \
  --worker-count <n> \
  --extraction-type <type> \
  --deployment-id <deployment_id> \
  --app <fly_app> \
  --work-pool <work_pool> \
  --required-models <model1,model2>
```

If wakeup tooling is available, schedule it for 300 seconds. Otherwise print the prompt and tell the operator to run it after 5 minutes.

## Confirmation Summary

Before mutation, show:

```text
Ready to dispatch:
  Type:
  App:
  Batch:
  Brand:
  Required models:
  Remaining:
  Workers:
  Limit per run:
  Delay between runs:
  Trigger scoring:
  Capture products:
  Capture entities:
  Account pool:
```
