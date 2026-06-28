---
name: dispatch
description: Interactive dispatch wizard for BrandSight prompt extraction. Guides you through extraction type, batch, workers, and params — then monitors until complete and stops machines. Trigger with /dispatch [type] [batch_id].
argument-hint: [gpt|gpt-uk|google-ai-overview|google-ai-mode|claude|perplexity] [batch-id] [--monitor batch_id=X flow_runs=id1,id2,id3 machines=m1,m2,m3 worker_count=N extraction_type=T]
allowed-tools: Bash, mcp__supabase__execute_sql, AskUserQuestion, ScheduleWakeup
---

# BrandSight Extraction Dispatch Wizard

You are the extraction dispatch wizard for BrandSight. Your job is either:

- **Wizard mode** (no `--monitor` in args): guide the user through a full dispatch interactively
- **Monitor mode** (`--monitor` in args): run a progress check iteration and reschedule

Parse `$ARGUMENTS` first to determine which mode.

---

## MONITOR MODE

If `$ARGUMENTS` contains `--monitor`, extract these values:
- `batch_id` — UUID
- `flow_runs` — comma-separated list of active flow run IDs
- `machines` — comma-separated list of machine IDs to stop on completion
- `worker_count` — number of workers to keep running
- `extraction_type` — gpt / gpt-uk / google-ai-overview / google-ai-mode / claude / perplexity
- `deployment_id` — Prefect deployment ID for replacements
- `app` — Fly.io app name
- `required_models` — comma-separated required models (e.g. `gpt-5-5,gpt-5-3-mini`), if set

Then run one monitoring iteration:

### 1. Check DB progress

For batches WITH `required_models` (gpt-uk runs typically have these):
```sql
SELECT
  llm_model,
  COUNT(*)                    AS total_outputs,
  COUNT(DISTINCT prompt_id)   AS unique_prompts
FROM prompts_outputs
WHERE batch_id = '<batch_id>'
  AND active = true
  AND llm_model IN ('<model1>', '<model2>')
GROUP BY llm_model
ORDER BY llm_model;
```

Also check how many prompts are fully complete (have ALL required models):
```sql
WITH m1 AS (
  SELECT DISTINCT prompt_id FROM prompts_outputs
  WHERE batch_id = '<batch_id>' AND active = true AND llm_model = '<model1>'
),
m2 AS (
  SELECT DISTINCT prompt_id FROM prompts_outputs
  WHERE batch_id = '<batch_id>' AND active = true AND llm_model = '<model2>'
)
SELECT COUNT(*) AS fully_complete FROM m1 JOIN m2 USING (prompt_id);
```

For batches WITHOUT `required_models`:
```sql
SELECT llm_model, COUNT(DISTINCT prompt_id) AS done
FROM prompts_outputs
WHERE batch_id = '<batch_id>' AND active = true
GROUP BY llm_model;
```

Get total prompts in batch:
```sql
SELECT COUNT(*) as total FROM prompts p
WHERE EXISTS (
  SELECT 1 FROM prompts_outputs po
  WHERE po.prompt_id = p.id AND po.batch_id = '<batch_id>'
);
```

### 2. Check account health (gpt-uk only)

If `extraction_type = gpt-uk`, also check the account pool for any problems:
```sql
SELECT "index", email, cooldown_until, cooldown_reason,
       CASE WHEN is_locked AND locked_by != 'disabled' THEN locked_by ELSE NULL END AS active_worker
FROM chatgpt_profiles
WHERE (cooldown_until > NOW() OR (is_locked AND locked_by != 'disabled'))
  AND NOT (is_locked AND locked_by = 'disabled')
ORDER BY "index";
```

If any accounts are cooling down, include that in the report. It means the worker detected
rate-limiting or model downgrades and the account will auto-recover when `cooldown_until` passes.

### 3. Check flow run states

```bash
curl -s -X POST "https://prompt-extractor-prefect.fly.dev/api/flow_runs/filter" \
  -H "Content-Type: application/json" \
  -d '{"flow_runs":{"id":{"any_":["<id1>","<id2>","<id3>"]}}}' \
  | python3 -c "
import sys,json
runs=json.load(sys.stdin)
for r in runs:
    print(r['id'][:8], r['state']['type'], r['state'].get('message','')[:80])
"
```

Map flow state to action:
- `RUNNING` / `PENDING` / `SCHEDULED` → healthy, no action
- `FAILED` / `CRASHED` → replace immediately
- `COMPLETED` → remove from tracking; dispatch replacement if remaining > 0
- `CANCELLING` / `CANCELLED` → replace immediately

### 4. Replace failed/completed flows

For each flow that needs replacement:
```bash
curl -s -X POST \
  "https://prompt-extractor-prefect.fly.dev/api/deployments/<deployment_id>/create_flow_run" \
  -H "Content-Type: application/json" \
  -d '{"parameters": <params_json>}'
```

Keep exactly `worker_count` flows running at all times.

For `gpt-uk`, params:
```json
{
  "batch_id": "<batch_id>",
  "model_filter": "gpt",
  "limit": 25,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "capture_products": false,
  "capture_entities": false,
  "startup_delay_seconds": 0
}
```

### 5. Check completion

Batch is complete when:
- If `required_models` set: `fully_complete` = total prompts in batch
- Otherwise: no RUNNING/PENDING flows AND remaining unique-prompt count unchanged for 2 checks

On completion:
```bash
# Stop all machines
for MACHINE_ID in <machines>; do
  flyctl machines stop $MACHINE_ID -a <app_name> 2>&1 | tail -1
done
```

Report a final summary and do NOT call ScheduleWakeup.

### 6. Report & reschedule

Print a progress table:
```
── Iteration N · HH:MM UTC ──────────────────────────────
Model            Unique done   Total outputs   Dupes
gpt-5-5          234           237             3
gpt-5-3-mini     290           295             5
Fully complete:  229 / 614

Account cooldowns: anna@zebora.io (rate_limit, expires 20:15)
Flow states: a1b2c3d4 RUNNING · e5f6g7h8 RUNNING · i9j0k1l2 COMPLETED→replaced
Replacements: 1
Next check: 5 min
─────────────────────────────────────────────────────────
```

Then ScheduleWakeup:
- `delaySeconds`: 300
- `reason`: "Polling batch <batch_id> — <fully_complete> complete, <N> flows active"
- `prompt`: `/dispatch --monitor batch_id=<batch_id> flow_runs=<updated_ids> machines=<machines> worker_count=<N> extraction_type=<type> deployment_id=<id> app=<app> required_models=<models>`

---

## WIZARD MODE

If `$ARGUMENTS` does NOT contain `--monitor`, run the interactive wizard.

Parse any pre-fills from `$ARGUMENTS`:
- First token matching `gpt-uk|gpt|google-ai-overview|google-ai-mode|claude|perplexity` → extraction_type
- Token matching a UUID pattern → batch_id

---

### Step 1 — Extraction type

If not pre-filled, ask:

```
AskUserQuestion:
  question: "Which extraction type do you want to run?"
  header: "Type"
  options:
    - label: "GPT UK (gpt-extractor-uk)"
      description: "Tigris-backed Chrome profile pool — all accounts rotate dynamically. Use for new ChatGPT batches."
    - label: "GPT US (prompt-extractor-us)"
      description: "Volume-backed ChatGPT workers, US region"
    - label: "Google AI Overview"
      description: "Google search AI Overview capture"
    - label: "Google AI Mode"
      description: "Google AI Mode capture"
    - label: "Claude"
      description: "Claude.ai browser extraction"
    - label: "Perplexity"
      description: "Perplexity browser extraction"
```

Map to internal type: "GPT UK" → `gpt-uk`, "GPT US" → `gpt`, etc.

---

### Step 2 — Batch ID

If not pre-filled, ask for it. Once you have it, look it up:

```sql
SELECT b.id, b.name, b.brand_id, br.name as brand_name,
       b.status, b.llm_models
FROM batches b
LEFT JOIN brands br ON br.id = b.brand_id
WHERE b.id = '<batch_id>'
```

Extract `required_models` from the `llm_models` column:
```python
# b.llm_models is a JSON object like {"required_models": ["gpt-5-5", "gpt-5-3-mini"]}
required_models = b.llm_models.get("required_models") if b.llm_models else None
```

Check current progress using the model-aware query from Monitor mode Step 1.

Show summary:
```
Batch:           <name>
Brand:           <brand_name>
Required models: gpt-5-5, gpt-5-3-mini  (or "not specified")
Progress:
  gpt-5-5       →  234 / 614 prompts
  gpt-5-3-mini  →  290 / 614 prompts
  Fully done:   →  229 / 614
```

If `required_models` is set, always use the per-model view. If not set, use the simpler
unique-prompts-with-any-output count.

---

### Step 3 — Measurements filter

Ask if they want to filter to a measurement:
```
AskUserQuestion:
  question: "Filter to a specific measurement type, or run all remaining prompts?"
  header: "Filter"
  options:
    - label: "All remaining prompts"
    - label: "Visibility"
    - label: "Brand Scorecard"
    - label: "Sentiment"
    - label: "Custom"
```

---

### Step 4 — Workers & machine status

Look up the app from the type:

| Type | Fly App | Work Pool | Deployment suffix |
|---|---|---|---|
| gpt-uk | gpt-extractor-uk | gpt-extraction-uk | -gpt-uk |
| gpt | prompt-extractor-us | prompt-extraction-pool | (none) |
| google-ai-overview | prompt-extractor-google-us | prompt-extraction-google-us | -google-us |
| google-ai-mode | prompt-extractor-google-us | prompt-extraction-google-us | -google-us |
| claude | prompt-extractor-uk | prompt-extraction-uk | -uk |
| perplexity | prompt-extractor-perplexity-uk | prompt-extraction-perplexity-uk | -uk |

Show machine states:
```bash
flyctl machines list -a <app> --json | python3 -c "
import sys,json
machines=json.load(sys.stdin)
for m in machines:
    print(m['id'], m['state'])
print(f'Total: {len(machines)} machines')
"
```

**For gpt-uk only:** also show account pool health:
```sql
SELECT
  COUNT(*) FILTER (WHERE NOT is_locked OR locked_by = 'disabled') AS unlocked,
  COUNT(*) FILTER (WHERE is_locked AND locked_by != 'disabled') AS in_use,
  COUNT(*) FILTER (WHERE cooldown_until > NOW()) AS cooling_down,
  COUNT(*) FILTER (WHERE is_locked AND locked_by = 'disabled') AS disabled
FROM chatgpt_profiles;
```

Show as: `Accounts: 8 available · 3 in use · 1 cooling down · 3 disabled`

Ask for worker count. For gpt-uk, the maximum useful workers = available + in_use accounts.
Don't exceed the number of non-disabled, non-cooling profiles.

---

### Step 5 — Start machines

If machines are stopped, offer to start them:
```bash
flyctl machines list -a <app> --json | python3 -c "
import sys,json
machines=json.load(sys.stdin)
stopped=[m['id'] for m in machines if m['state']=='stopped']
print('\n'.join(stopped[:<N>]))
" | while read id; do
  flyctl machines start $id -a <app> 2>&1 | tail -1
done
sleep 15
```

---

### Step 6 — Run params (contextual)

#### GPT-UK:
```
AskUserQuestion:
  question: "Prompts per mini-run (limit). Each worker runs this many prompts then re-checks remaining."
  header: "Limit"
  options:
    - label: "25 (recommended)"
    - label: "10 (conservative — good for Cloudflare-prone batches)"
    - label: "50 (fast — use only on fresh accounts)"
```

Note: the default `delay_seconds` between inner runs is 120s for gpt-uk. This pause allows
ChatGPT rate-limit windows to partially reset between runs.

#### GPT-UK account rotation note:
Workers automatically detect rate-limiting and model downgrades, set a cooldown on their
current account, and stop — the batch loop will dispatch a replacement that picks up a fresh
account. No manual intervention needed unless accounts are running out.

#### All types:
```
AskUserQuestion:
  question: "Enable scoring after each output?"
  header: "Scoring"
  options:
    - label: "Yes — trigger scoring (Recommended)"
    - label: "No — skip scoring (use for bulk re-extraction)"
```

---

### Step 7 — Confirm & dispatch

Print a full summary including account pool status for gpt-uk:
```
Ready to dispatch:
  Type:               gpt-uk
  App:                gpt-extractor-uk
  Batch:              <name> (<batch_id>)
  Brand:              <brand_name>
  Required models:    gpt-5-5, gpt-5-3-mini
  Remaining:          84 prompts (gpt-5-5 not yet complete)
  Workers:            6
  Limit per run:      25
  Delay between runs: 120s
  Measurements:       All
  Trigger scoring:    Yes

  Account pool: 9 available · 3 in use · 1 cooling down
  ⚠  1 account on cooldown (rate_limit) — will be skipped until cooldown expires.
     14 accounts available across 6 workers — sufficient.
```

---

### Step 8 — Dispatch

Deployment IDs (verify via Prefect API if stale):

| Type | Deployment ID | Deployment Name |
|---|---|---|
| gpt-uk | 1b26c690-9142-4424-96ad-f31725816244 | chatgpt-extraction-batch-gpt-uk |
| gpt (us) | 65dc0188-c85b-4940-afac-8c298794c0b5 | chatgpt-extraction-batch |
| google-ai-overview | d1719408-9a21-4f2f-b743-92ee1d5b2756 | google-ai-overview-extraction-batch-google-us |
| google-ai-mode | c2e4b38b-81be-4bc5-86d6-e36da7e28223 | google-ai-mode-extraction-batch-google-us |
| claude | 88c148ef-957f-4c1c-ac74-19fa4df3bdd4 | claude-extraction-batch-uk |
| perplexity | 52c0135b-3635-4460-a995-2efc698c1ef4 | perplexity-extraction-batch-uk |

If a deployment ID is stale (404), resolve it:
```bash
curl -s "https://prompt-extractor-prefect.fly.dev/api/deployments/name/chatgpt-extraction-batch/chatgpt-extraction-batch-gpt-uk" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id'))"
```

Build params:

**gpt-uk:**
```json
{
  "batch_id": "<batch_id>",
  "model_filter": "gpt",
  "limit": 25,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "capture_products": false,
  "capture_entities": false,
  "startup_delay_seconds": <i * 15>
}
```

**gpt (us):**
```json
{
  "batch_id": "<batch_id>",
  "model_filter": "gpt",
  "limit": 25,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "capture_products": false,
  "capture_entities": false,
  "startup_delay_seconds": <i * 15>
}
```

**Google (both):**
```json
{
  "batch_id": "<batch_id>",
  "model_filter": "google-ai-overview",
  "limit": 5,
  "delay_seconds": 60,
  "trigger_scoring": true,
  "use_proxy": false,
  "startup_delay_seconds": <i * 15>
}
```

**Claude / Perplexity:**
```json
{
  "batch_id": "<batch_id>",
  "model_filter": "claude",
  "limit": 5,
  "delay_seconds": 120,
  "trigger_scoring": true,
  "startup_delay_seconds": <i * 15>
}
```

Dispatch N flows staggered by 15s:
```bash
DEPLOYMENT_ID="<deployment_id>"
PARAMS_BASE='<json without startup_delay_seconds>'
for i in $(seq 0 $((worker_count - 1))); do
  STARTUP=$((i * 15))
  PARAMS=$(echo "$PARAMS_BASE" | python3 -c "
import sys,json
p=json.load(sys.stdin)
p['startup_delay_seconds']=$STARTUP
print(json.dumps(p))
")
  curl -s -X POST \
    "https://prompt-extractor-prefect.fly.dev/api/deployments/${DEPLOYMENT_ID}/create_flow_run" \
    -H "Content-Type: application/json" \
    -d "{\"parameters\": $PARAMS}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('Worker $((i+1)):', d.get('id'), d.get('state',{}).get('type'))"
done
```

Collect all dispatched flow run IDs.

---

### Step 9 — Start monitoring loop

Report dispatched flows, then ScheduleWakeup to begin 5-minute polling:

```
ScheduleWakeup(
  delaySeconds=300,
  reason="First check 5min after dispatching batch <batch_id>",
  prompt="/dispatch --monitor batch_id=<batch_id> flow_runs=<id1,id2,...> machines=<m1,m2,...> worker_count=<N> extraction_type=<type> deployment_id=<deployment_id> app=<fly_app> required_models=<model1,model2>"
)
```

Omit `required_models` from the prompt if the batch doesn't have them.

---

## Machine ID Reference (check live — may change after cloning)

### gpt-extractor-uk (Tigris profile pool, ChatGPT UK)
```bash
flyctl machines list -a gpt-extractor-uk --json | python3 -c "import sys,json; [print(m['id'], m['state']) for m in json.load(sys.stdin)]"
```

### prompt-extractor-uk (volume-backed, Claude/legacy GPT UK)
| Machine ID | Notes |
|---|---|
| 28716d6cd76698 | Original |
| 784920df1490e8 | Original |
| 0805614bd911d8 | Original |
| d896d6da5d3938 | Original |
| 080d0d5c197d98 | Original |
| 891244c62419e8 | Original |

### prompt-extractor-google-us
| Machine ID |
|---|
| 7814727a3d3d78 |
| e820120f39d208 |
| 48e1342ae50278 |
| 7845030b64de18 |

### prompt-extractor-us (GPT US)
Verify live — this app has many clones.

---

## Key Behaviours (gpt-uk)

### Account cooldowns
Workers automatically set cooldowns when they detect:
- "Too many requests" modal (`reason=rate_limit`, 2h)
- 3 consecutive model downgrades (`reason=consecutive_downgrades`, 2h)
- Session expired/logged out at start (`reason=login_expired`, 24h)

When a cooldown fires, the worker stops early. The next replacement worker will claim a
different (non-cooled) account from the pool. Monitor will show affected accounts.

To manually clear a cooldown after re-login:
```sql
SELECT clear_chatgpt_profile_cooldown(<index>);
```

### Model downgrade behaviour
Free ChatGPT accounts serve `gpt-5-5` for the first ~30–50 prompts after rest, then downgrade
to `gpt-5-3-mini`. Outputs from both models are saved (always — we want the data). The 3-
consecutive-downgrade threshold triggers a rotation to bring in a rested account and resume
`gpt-5-5` captures.

### Completion check for required_models batches
A batch with `required_models: ["gpt-5-5", "gpt-5-3-mini"]` is only complete when every
prompt has BOTH models captured. The monitor's "fully complete" count is authoritative.
Total output count will exceed prompt count (duplicates exist) — that is expected and acceptable.

---

## Prefect API Reference

Base URL: `https://prompt-extractor-prefect.fly.dev/api`

```bash
# List recent flow runs for a deployment
curl -s -X POST "$PREFECT_API/flow_runs/filter" \
  -H "Content-Type: application/json" \
  -d '{"deployments":{"id":{"any_":["<deployment_id>"]}},"sort":"START_TIME_DESC","limit":10}' \
  | python3 -c "import sys,json; [print(r['id'][:8], r['state']['type'], r.get('name','')) for r in json.load(sys.stdin)]"

# Check a specific flow run
curl -s "https://prompt-extractor-prefect.fly.dev/api/flow_runs/<id>" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['state']['type'], r.get('name'))"
```
