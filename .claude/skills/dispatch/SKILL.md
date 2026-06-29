---
name: dispatch
description: Interactive dispatch wizard for BrandSight prompt extraction. Guides you through extraction type, batch, workers, and params — then monitors until complete and stops machines. Trigger with /dispatch [type] [batch_id].
argument-hint: [gpt|gpt-uk|google-ai-overview|google-ai-mode|claude|perplexity] [batch-id] [--monitor batch_id=X flow_runs=id1,id2,id3 machines=m1,m2,m3 worker_count=N extraction_type=T]
allowed-tools: Bash, mcp__supabase__execute_sql, AskUserQuestion, ScheduleWakeup, PushNotification
# Note: Supabase writes (lock releases) use supabase-py via Bash, not mcp__supabase__execute_sql (read-only)---

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
- `prev_output_counts` — (optional) `index:last24h_total` pairs from previous iteration, e.g. `9:42,5:21`
- `zero_output_accounts` — (optional) `index:consecutive_zero_iterations` pairs, e.g. `8:1,5:2`
- `prev_model_counts` — (optional) `model:unique_done` pairs from previous iteration, e.g. `gpt-5-5:333,gpt-5-3-mini:635`

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

If `extraction_type = gpt-uk`, query the `chatgpt_profile_stats` view for a full account snapshot:
```sql
SELECT "index", email, status, worker, cooldown_until, cooldown_reason,
       last24h_gpt55, last24h_mini, last24h_total, last24h_gpt55_pct
FROM chatgpt_profile_stats
ORDER BY last24h_total DESC;
```

Include this table in the Step 6 report. It shows which accounts are hot (high `last24h_total`),
cooling down, or downgraded — so each monitor iteration gives a full picture without a separate query.

#### 2a. Stale lock detection (gpt-uk only)

After getting account stats, check for locks held by machines that are NOT in the active `machines=` list.
These are orphaned locks from dead processes that will never self-release.

Run the following Python to detect and auto-release stale locks:
```python
import os, sys
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

url = os.environ.get('BRANDSIGHT_SUPABASE_URL') or 'https://hmwgplzdzffivawkflci.supabase.co'
key = os.environ.get('BRANDSIGHT_SUPABASE_SERVICE_KEY')
client = create_client(url, key)

active_machines = ['<machine_id_1>', '<machine_id_2>', ...]  # from machines= arg

# Find accounts locked by machines NOT in the active list
result = client.table('chatgpt_profiles').select(
    '"index", email, locked_by, locked_at'
).eq('is_locked', True).neq('locked_by', 'disabled').execute()

stale = [r for r in result.data if r['locked_by'] not in active_machines]
if stale:
    stale_indices = [r['index'] for r in stale]
    client.table('chatgpt_profiles').update({
        'is_locked': False, 'locked_by': None, 'locked_at': None, 'lock_expires_at': None
    }).in_('"index"', stale_indices).execute()
    for r in stale:
        print(f"Auto-released stale lock: index={r['index']} email={r['email']} was held by {r['locked_by']}")
else:
    print("No stale locks found.")
```

Report any auto-released locks in the iteration output with ⚠ prefix.

#### 2b. Zero-output account detection (gpt-uk only)

After getting account stats, compare current `last24h_total` for each `in_use` account against
the `prev_output_counts=` values from the previous iteration. Track how many consecutive
iterations an account has shown no new output.

```python
# Parse state from prompt args
prev_counts = {}  # {index: last24h_total} from prev_output_counts= arg
zero_counts = {}  # {index: consecutive_zero_iterations} from zero_output_accounts= arg

# For each currently in_use account:
#   current_total = last24h_total from chatgpt_profile_stats
#   prev_total = prev_counts.get(index, None)
#   if prev_total is not None and current_total == prev_total:
#       zero_counts[index] = zero_counts.get(index, 0) + 1
#   else:
#       zero_counts.pop(index, None)  # reset counter if output increased

# Accounts with zero_counts[index] >= 2 → trigger rotation (see Step 4b)
accounts_to_rotate = [idx for idx, cnt in zero_counts.items() if cnt >= 2]

# Build updated prev_output_counts for next iteration (in_use accounts only)
new_prev_counts = {r['index']: r['last24h_total'] for r in stats if r['status'] == 'in_use'}
```

### 3. Reconcile active flows from deployment (Fix 4)

**Always query the deployment's live flow runs first**, then reconcile with tracked IDs.
This catches flows dispatched outside the monitor's view and drops phantom stale IDs.

```bash
curl -s -X POST "https://prompt-extractor-prefect.fly.dev/api/flow_runs/filter" \
  -H "Content-Type: application/json" \
  -d '{
    "deployments": {"id": {"any_": ["<deployment_id>"]}},
    "sort": "START_TIME_DESC",
    "limit": 15
  }' | python3 -c "
import sys, json
from datetime import datetime, timezone

runs = json.load(sys.stdin)
now = datetime.now(timezone.utc)
tracked = set('<flow_runs_from_prompt>'.split(','))

live = []
for r in runs:
    state = r['state']['type']
    rid = r['id']
    created = datetime.fromisoformat(r['created'].replace('Z', '+00:00'))
    age_min = (now - created).total_seconds() / 60
    if state in ('RUNNING', 'SCHEDULED', 'PENDING'):
        live.append({'id': rid, 'state': state, 'age_min': round(age_min, 1)})
    elif rid in tracked and state in ('CANCELLED', 'COMPLETED', 'FAILED', 'CRASHED'):
        print(f'DROP {rid[:8]} — {state} (was tracked)')

# Union: live from deployment + any tracked IDs still in RUNNING/SCHEDULED
print('LIVE:', json.dumps(live))
"
```

**Reconciliation rules:**
1. Start with all live RUNNING/SCHEDULED/PENDING flows from the deployment query
2. Add any tracked IDs not in the deployment results that are still RUNNING (they may have been dispatched very recently)
3. Drop any tracked ID that is CANCELLED, COMPLETED, FAILED, or CRASHED
4. The reconciled set becomes the authoritative flow list for this iteration

Note flows that were SCHEDULED for >15 minutes — these indicate a blocked worker slot (Fix 1).

```python
# After reconciliation, check for stuck SCHEDULED flows
stuck_scheduled = [f for f in live_flows if f['state'] == 'SCHEDULED' and f['age_min'] > 15]
```

### 4. Check flow states and replace

Using the reconciled flow list from Step 3:

Map flow state to action:
- `RUNNING` / `PENDING` → healthy, no action
- `SCHEDULED` and age < 15 min → healthy, allow time to be picked up
- `SCHEDULED` and age ≥ 15 min → **blocked worker slot** — trigger machine cycle (see 4a)
- `FAILED` / `CRASHED` → replace immediately
- `COMPLETED` → remove from tracking; dispatch replacement if remaining > 0
- `CANCELLING` / `CANCELLED` → replace immediately

#### Opt 6: Dynamic worker count scale-down

Before dispatching replacements, compute the effective worker count based on remaining work.
As the batch nears completion, fewer workers are needed — holding extra accounts locked is wasteful.

```python
remaining = total_prompts - fully_complete
limit = 25  # prompts per worker run (from original dispatch params)
effective_workers = max(1, min(worker_count, math.ceil(remaining / limit)))
```

If `effective_workers < worker_count`:
- Do NOT dispatch a replacement when a COMPLETED flow finishes — let the active count naturally drain to `effective_workers`
- Report the scale-down: `Scaling 3→1 workers (22 prompts remaining)`

Only dispatch up to `effective_workers` flows at any time.

#### 4a. Machine cycle for blocked/cancelled workers (Fix 1)

When replacing a flow that was SCHEDULED ≥15 min, CANCELLED, or CRASHED — the old Python
process may still be blocking the Prefect worker slot on the machine. Always stop/start the
machine before dispatching the replacement to guarantee a clean worker slot.

```bash
# For each machine associated with a flow needing replacement:
# (For gpt-uk: check which machine holds the lock for the account that was on that flow.
#  If unknown, cycle ALL machines that have no current RUNNING flow.)

flyctl machines stop <machine_id> -a <app> 2>&1 | tail -1
sleep 5
flyctl machines start <machine_id> -a <app> 2>&1 | tail -1
sleep 15  # allow Prefect worker process to reconnect before dispatching
```

Then dispatch the replacement flow:
```bash
curl -s -X POST \
  "https://prompt-extractor-prefect.fly.dev/api/deployments/<deployment_id>/create_flow_run" \
  -H "Content-Type: application/json" \
  -d '{"parameters": <params_json>}'
```

For `gpt-uk`, replacement params:
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

#### 4b. Zero-output account rotation (Fix 2)

For each account index in `accounts_to_rotate` (from Step 2b — 2+ consecutive zero-output iterations):

1. **Find and cancel its flow** — the account's `worker` field in chatgpt_profile_stats is the machine ID; check which tracked flow is RUNNING on that machine
2. **Release the profile lock:**
```python
client.table('chatgpt_profiles').update({
    'is_locked': False, 'locked_by': None, 'locked_at': None, 'lock_expires_at': None
}).eq('"index"', stale_index).execute()
```
3. **Cycle the machine** (Fix 1 pattern — stop/start)
4. **Dispatch a replacement flow**
5. **Remove from zero_counts** — reset counter for this account

Report auto-rotations with ⚠ prefix in the iteration output.

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

On completion:
1. Stop all machines
2. Send `PushNotification` with a summary: batch name, model counts, total time
3. Report a final summary and do NOT call ScheduleWakeup (loop ends)

### 6. Report & reschedule

#### Opt 1: Δ (delta) tracking

Parse `prev_model_counts=` from the prompt args to compute output change since the last iteration:
```python
prev = {}  # {model: unique_done} from prev_model_counts= arg
delta = {model: current_done - prev.get(model, current_done) for model, current_done in current_counts.items()}
stalled_models = [m for m, d in delta.items() if d == 0]
```

Print the progress table with Δ column:
```
── Iteration N · HH:MM UTC ──────────────────────────────
Model          Done    Δ    Total   Dupes
gpt-5-5         345   +12   345     0
gpt-5-3-mini    648    +9   648     3
Fully complete:  218 / 759   Δ+10

⚠ STALLED: gpt-5-5 showed 0 new outputs this iteration
```

Show `⚠ STALLED` if any required model had Δ=0 this iteration. If Δ=0 for 2 consecutive
iterations on the same model, escalate: `🚨 STALLED 2 ITERATIONS — consider forced rotation`.

Account pool (last 24h):
```
#   Email                  Status     Worker            24h-55  24h-mini  Total  55%    Zero-iters
9   anna@zebora.io         in_use     7845165c921478    42      18        60     70%    —
8   john@zebora.io         in_use     7845142f919668    0       0         0      —      ⚠2 (→rotating)
2   bob@zebora.io          cooldown   —                 31      12        43     72%    —
...
```

Add `Zero-iters` column for in_use accounts (from `zero_output_accounts=`).
Accounts with `status = 'cooldown'` show `cooldown_reason` in the Status column.
Omit this table for non-gpt-uk extraction types.

Auto-action notes:
```
⚠ Auto-released stale lock: index=17 (emily) was held by dead machine d896d6da7573e8
⚠ Auto-rotating index=8 (john) — 2 consecutive zero-output iterations
Scaling 3→1 workers (22 prompts remaining)
```

Flow states:
```
Flow states: a1b2c3d4 RUNNING · e5f6g7h8 RUNNING (age:3min) · i9j0k1l2 SCHEDULED→blocked(18min)→cycling+replaced
Reconciled from deployment: added f9g8h7i6 (newly RUNNING, not previously tracked)
Replacements: 2 · Effective workers: 2/3
Next check: 5 min
Prefect UI: https://prompt-extractor-prefect.fly.dev/runs?state=RUNNING
─────────────────────────────────────────────────────────
```

#### Opt 5: Explicit stop — write resume state

If the user asks to stop/pause the monitor mid-batch (not because it completed), before stopping
machines write a resume state file:

```bash
cat > .brandsight-resume.json << 'EOF'
{
  "batch_id": "<batch_id>",
  "paused_at": "<ISO timestamp>",
  "fully_complete": <N>,
  "total": <total>,
  "gpt55_done": <N>,
  "mini_done": <N>,
  "machines": ["<m1>", "<m2>", ...],
  "extraction_type": "<type>",
  "deployment_id": "<id>",
  "app": "<app>",
  "required_models": ["<model1>", "<model2>"],
  "resume_prompt": "/dispatch --monitor batch_id=<batch_id> flow_runs=<ids> machines=<machines> worker_count=<N> extraction_type=<type> deployment_id=<id> app=<app> required_models=<models>"
}
EOF
```

Print the `resume_prompt` value so it can be copied directly.

On the next `/dispatch` invocation (wizard mode), if `.brandsight-resume.json` exists, offer:
```
Resume state found: Range Rover Jun-2026 — 210/759 complete (paused 2026-06-29T16:01Z)
Resume the paused batch? [Yes / Start fresh]
```

#### Reschedule

Call ScheduleWakeup with the **updated** flow run IDs (use reconciled IDs from Step 3):

- `delaySeconds`: 300
- `reason`: "Polling batch <batch_id> — <done> / <total> complete, <N> flows active"
- `prompt`: `/dispatch --monitor batch_id=<batch_id> flow_runs=<reconciled_ids> machines=<machines> worker_count=<N> extraction_type=<type> deployment_id=<id> app=<app> required_models=<models> prev_output_counts=<index:total,...> zero_output_accounts=<index:count,...> prev_model_counts=<model:done,...>`

Omit `required_models` from the prompt if the batch doesn't have them.
Omit `prev_output_counts`, `zero_output_accounts`, `prev_model_counts` if empty.
Only include `zero_output_accounts` for accounts still in_use with non-zero counters (reset on rotation).

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

#### GPT-UK only — additional capture options:
```
AskUserQuestion:
  question: "Which additional data types should be captured alongside the response?"
  header: "Capture"
  multiSelect: true
  options:
    - label: "Products"
      description: "Extract product mentions from the response (capture_products=true)"
    - label: "Entities"
      description: "Extract named entities from the response (capture_entities=true)"
```

If the user selects neither, both default to false. Map selections to:
- "Products" selected → `capture_products: true`
- "Entities" selected → `capture_entities: true`

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
  Capture products:   Yes / No
  Capture entities:   Yes / No

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
  "limit": <limit>,
  "delay_seconds": 120,
  "trigger_scoring": <trigger_scoring>,
  "capture_products": <capture_products>,
  "capture_entities": <capture_entities>,
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

Report dispatched flows, then schedule the first monitor iteration via `ScheduleWakeup`.
Each monitor iteration (Step 6) rebuilds the prompt with updated flow IDs before rescheduling,
so replacements are always tracked correctly.

Build the monitor prompt (a single string with all state inline):
```
/dispatch --monitor batch_id=<batch_id> flow_runs=<id1,id2,...> machines=<m1,m2,...> worker_count=<N> extraction_type=<type> deployment_id=<deployment_id> app=<fly_app> required_models=<model1,model2>
```

Omit `required_models` if the batch doesn't have them.

Then call ScheduleWakeup directly:
- `delaySeconds`: 300
- `reason`: "First monitor check for batch <batch_id> — <N> workers dispatched"
- `prompt`: the monitor prompt above

**Do NOT use the `/loop` skill here.** The loop skill uses `CronCreate` with a static prompt,
which cannot update flow IDs when replacements are dispatched. `ScheduleWakeup` is the correct
mechanism — monitor mode rebuilds the prompt with current IDs on every iteration.
When the monitor detects batch completion (all `fully_complete = total`):
1. Stop all machines
2. Call `PushNotification` with a summary: batch name, total captured, time taken
3. Report a final summary and do NOT call ScheduleWakeup (loop ends)
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

### Forced rotation procedure

Use when an account is stuck (zero output), a flow is blocked, or you want to swap out a
specific account for a fresher one. Steps:

1. **Cancel the stuck flow** via Prefect API (CANCELLING state)
2. **Release the profile lock** via supabase-py:
   ```python
   import os
   from dotenv import load_dotenv; load_dotenv()
   from supabase import create_client
   client = create_client(
       os.environ.get('BRANDSIGHT_SUPABASE_URL') or 'https://hmwgplzdzffivawkflci.supabase.co',
       os.environ['BRANDSIGHT_SUPABASE_SERVICE_KEY']
   )
   client.table('chatgpt_profiles').update({
       'is_locked': False, 'locked_by': None, 'locked_at': None, 'lock_expires_at': None
   }).eq('"index"', <account_index>).execute()
   ```
3. **Stop the machine** to kill the old Python process:
   ```bash
   flyctl machines stop <machine_id> -a <app> 2>&1 | tail -1
   sleep 5
   flyctl machines start <machine_id> -a <app> 2>&1 | tail -1
   sleep 15
   ```
4. **Dispatch a replacement flow** — a fresh worker will claim a rested account

**Why the stop/start is mandatory:** Cancelling a Prefect flow does not kill the machine's
Python process. The old process keeps running and holds the Prefect worker slot, so any newly
SCHEDULED replacement flow never gets picked up. The stop/start guarantees a clean slot.

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
