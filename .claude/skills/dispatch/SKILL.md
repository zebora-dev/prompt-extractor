---
name: dispatch
description: Interactive dispatch wizard for BrandSight prompt extraction. Guides you through extraction type, batch, workers, and params — then monitors until complete and stops machines. Trigger with /dispatch [type] [batch_id].
argument-hint: [gpt|google-ai-overview|google-ai-mode|claude|perplexity] [batch-id] [--monitor batch_id=X flow_runs=id1,id2,id3 machines=m1,m2,m3 worker_count=N extraction_type=T]
allowed-tools: Bash, mcp__supabase__execute_sql, AskUserQuestion, ScheduleWakeup
---

# BrandSight Extraction Dispatch Wizard

You are the extraction dispatch wizard for BrandSight. Your job is either:

- **Wizard mode** (no `--monitor` in args): guide the user through a full dispatch interactively
- **Monitor mode** (`--monitor` in args): run a progress check iteration and reschedule

Parse `$ARGUMENTS` first to determine which mode.

---

## MONITOR MODE

If `$ARGUMENTS` contains `--monitor`, extract these values from the args string:
- `batch_id` — UUID
- `flow_runs` — comma-separated list of active flow run IDs
- `machines` — comma-separated list of machine IDs to stop on completion
- `worker_count` — number of workers to keep running
- `extraction_type` — gpt / google-ai-overview / google-ai-mode / claude / perplexity
- `deployment_id` — Prefect deployment ID for replacements

Then run one monitoring iteration:

### 1. Check DB progress

```sql
SELECT llm_model, COUNT(DISTINCT prompt_id) as completed
FROM prompts_outputs
WHERE batch_id = '<batch_id>'
  AND active = true
GROUP BY llm_model
ORDER BY llm_model
```

Also get total remaining:
```sql
SELECT COUNT(*) as remaining
FROM prompts p
WHERE p.id IN (
  SELECT DISTINCT po.prompt_id FROM prompts_outputs po
  WHERE po.batch_id = '<batch_id>'
)
AND p.id NOT IN (
  SELECT DISTINCT po.prompt_id FROM prompts_outputs po
  WHERE po.batch_id = '<batch_id>'
    AND po.active = true
    AND po.llm_model IN (
      SELECT DISTINCT llm_model FROM prompts_outputs
      WHERE batch_id = '<batch_id>' AND active = true
    )
)
```

If that's too complex, use a simpler proxy: compare total outputs vs unique prompts with any active output.

### 2. Check flow run states

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

### 3. Replace failed flows

For each flow in state FAILED or CRASHED:
- Dispatch a replacement using the same deployment and params
- Add the new flow run ID to the tracking list
- Remove the failed ID

Keep exactly `worker_count` flows running at all times.

Replacement dispatch:
```bash
curl -s -X POST \
  "https://prompt-extractor-prefect.fly.dev/api/deployments/<deployment_id>/create_flow_run" \
  -H "Content-Type: application/json" \
  -d '{"parameters": <same params as original>}'
```

### 4. Check completion

If all prompts are done (remaining = 0 or no active flows left and no new work):
- Stop the machines:
```bash
for MACHINE_ID in <machines>; do
  flyctl machines stop $MACHINE_ID -a <app_name> 2>&1 | tail -1
done
```
- Report completion summary
- Do NOT call ScheduleWakeup (ends the loop)

### 5. Report & reschedule

Print a progress table:
```
Iteration N — HH:MM
Model          | Completed
---------------|----------
gpt-5-5        | 37
gpt-5-3-mini   | 90

Flow states: <id1> RUNNING · <id2> RUNNING · <id3> RUNNING
Replacements dispatched: N
Next check: 5 minutes
```

Then call ScheduleWakeup:
- `delaySeconds`: 300
- `reason`: "Polling batch <batch_id> — <completed> done, checking for failures"
- `prompt`: `/dispatch --monitor batch_id=<batch_id> flow_runs=<updated_ids> machines=<machines> worker_count=<N> extraction_type=<type> deployment_id=<id>`

---

## WIZARD MODE

If `$ARGUMENTS` does NOT contain `--monitor`, run the interactive wizard.

Parse any pre-fills from `$ARGUMENTS`:
- First token matching `gpt|google-ai-overview|google-ai-mode|claude|perplexity` → extraction_type
- Token matching a UUID pattern → batch_id

---

### Step 1 — Extraction type

If not pre-filled, ask:

```
AskUserQuestion:
  question: "Which extraction type do you want to run?"
  header: "Type"
  options:
    - label: "GPT (ChatGPT browser)"
      description: "Uses prompt-extractor-uk machines with Chrome/Selenium"
    - label: "Google AI Overview"
      description: "Google search AI Overview capture — uses prompt-extractor-google-us"
    - label: "Google AI Mode"
      description: "Google AI Mode capture — uses prompt-extractor-google-us"
    - label: "Claude"
      description: "Claude.ai browser extraction — uses prompt-extractor-uk API pool"
    - label: "Perplexity"
      description: "Perplexity browser extraction — uses prompt-extractor-perplexity-uk"
```

Map answer to internal type:
- "GPT" → `gpt`
- "Google AI Overview" → `google-ai-overview`
- "Google AI Mode" → `google-ai-mode`
- "Claude" → `claude`
- "Perplexity" → `perplexity`

---

### Step 2 — Batch ID

If not pre-filled, ask:
```
AskUserQuestion:
  question: "Enter the batch ID (UUID) to run extraction for:"
  header: "Batch ID"
  options: [paste UUID] (allow Other for free text)
```

Once you have the batch_id, **look it up immediately**:

```sql
SELECT b.id, b.name, b.brand_id, br.name as brand_name,
       b.status, b.config
FROM batches b
LEFT JOIN brands br ON br.id = b.brand_id
WHERE b.id = '<batch_id>'
```

Also check current progress:
```sql
SELECT llm_model, COUNT(DISTINCT prompt_id) as done
FROM prompts_outputs
WHERE batch_id = '<batch_id>' AND active = true
GROUP BY llm_model
```

And total prompts:
```sql
SELECT COUNT(DISTINCT prompt_id) as total
FROM prompts_outputs
WHERE batch_id = '<batch_id>'
```

Show a summary:
```
Batch:    <name>
Brand:    <brand_name>
Status:   <status>
Progress: <done> / <total> prompts complete per model
```

If the batch doesn't exist, say so and stop.

---

### Step 3 — Measurements filter

Ask:
```
AskUserQuestion:
  question: "Filter to a specific measurement type, or run all prompts?"
  header: "Filter"
  options:
    - label: "All prompts"
      description: "Run every remaining prompt in the batch"
    - label: "Visibility"
      description: "Only prompts tagged to the Visibility measurement"
    - label: "Brand Scorecard"
      description: "Only prompts tagged to Brand Scorecard"
    - label: "Custom"
      description: "Enter a custom measurement name"
```

If "All prompts" → `measurements_filter = null`
If "Custom" → ask for the value via Other

---

### Step 4 — Workers

Look up the Fly.io app for the extraction type:

| Type | Fly App | Work Pool |
|---|---|---|
| gpt | prompt-extractor-uk | prompt-extraction-uk |
| google-ai-overview | prompt-extractor-google-us | prompt-extraction-google-us |
| google-ai-mode | prompt-extractor-google-us | prompt-extraction-google-us |
| claude | prompt-extractor-uk | prompt-extraction-api-uk |
| perplexity | prompt-extractor-perplexity-uk | prompt-extraction-api-uk |

Show current machine states:
```bash
flyctl machines list -a <app> --json | python3 -c "
import sys,json
machines=json.load(sys.stdin)
for m in machines:
    print(m['id'], m['state'])
print(f'Total: {len(machines)} machines')
"
```

Ask:
```
AskUserQuestion:
  question: "How many workers do you want to run? (X machines are currently stopped/started)"
  header: "Workers"
  options:
    - label: "1 worker"
    - label: "2 workers"
    - label: "3 workers"
    - label: "4 workers"
```

Then ask:
```
AskUserQuestion:
  question: "Should I start the machines now?"
  header: "Start machines"
  options:
    - label: "Yes — start them now (Recommended)"
      description: "Start the required machines and wait for workers to come online"
    - label: "No — they're already running"
      description: "Skip machine startup"
```

If "Yes", start the first N stopped machines:
```bash
# Start the first N stopped machines for this app
flyctl machines list -a <app> --json | python3 -c "
import sys,json
machines=json.load(sys.stdin)
stopped=[m['id'] for m in machines if m['state']=='stopped']
print('\n'.join(stopped[:<N>]))
" | while read id; do
  flyctl machines start $id -a <app> 2>&1 | tail -1
done
```

---

### Step 5 — Scraping params

Show contextual questions based on extraction type.

#### GPT only:
```
AskUserQuestion:
  question: "Which data should be captured alongside the response?"
  header: "Capture"
  multiSelect: true
  options:
    - label: "Products"
      description: "Capture product mentions (capture_products=true)"
    - label: "Entities"
      description: "Capture named entities (capture_entities=true)"
```

#### Google only:
```
AskUserQuestion:
  question: "Google search settings"
  header: "Google"
  options:
    - label: "Default (US, English, no proxy)"
    - label: "Use proxy"
```

#### All types:
```
AskUserQuestion:
  question: "Enable scoring after each run?"
  header: "Scoring"
  options:
    - label: "Yes — trigger scoring (Recommended)"
    - label: "No — skip scoring"
```

---

### Step 6 — Confirm & dispatch

Print a full summary:
```
Ready to dispatch:
  Type:               gpt
  Batch:              <name> (<batch_id>)
  Brand:              <brand_name>
  Workers:            3
  Measurements:       Visibility (or All)
  Capture products:   No
  Capture entities:   No
  Trigger scoring:    Yes
  Limit per run:      5
  Delay between runs: 120s

This will start 3 flow runs on prompt-extractor-uk.
```

Ask:
```
AskUserQuestion:
  question: "Ready to dispatch?"
  header: "Confirm"
  options:
    - label: "Dispatch now"
    - label: "Cancel"
```

If cancelled, stop.

---

### Step 7 — Dispatch

Look up the correct deployment ID:

| Type | Region | Deployment ID | Deployment Name |
|---|---|---|---|
| gpt | uk | 0b4315ac-e108-40b5-b085-b4f2329a95b2 | prompt-extraction-batch-uk |
| gpt | us | 65dc0188-c85b-4940-afac-8c298794c0b5 | chatgpt-extraction-batch |
| google-ai-overview | google-us | d1719408-9a21-4f2f-b743-92ee1d5b2756 | google-ai-overview-extraction-batch-google-us |
| google-ai-mode | google-us | c2e4b38b-81be-4bc5-86d6-e36da7e28223 | google-ai-mode-extraction-batch-google-us |
| claude | uk | 88c148ef-957f-4c1c-ac74-19fa4df3bdd4 | claude-extraction-batch-uk |
| perplexity | uk | 52c0135b-3635-4460-a995-2efc698c1ef4 | perplexity-extraction-batch-uk |

Build the parameters object based on type:

**GPT:**
```json
{
  "batch_id": "<batch_id>",
  "limit": 5,
  "delay_seconds": 120,
  "trigger_scoring": true/false,
  "capture_products": true/false,
  "capture_entities": true/false,
  "measurements_filter": "<value or omit>",
  "startup_delay_seconds": <0, 15, 30, 45...>
}
```

**Google:**
```json
{
  "batch_id": "<batch_id>",
  "limit": 5,
  "delay_seconds": 60,
  "trigger_scoring": true/false,
  "use_proxy": true/false,
  "measurements_filter": "<value or omit>",
  "startup_delay_seconds": <0, 15, 30...>
}
```

**Claude / Perplexity:**
```json
{
  "batch_id": "<batch_id>",
  "limit": 5,
  "delay_seconds": 120,
  "trigger_scoring": true/false,
  "measurements_filter": "<value or omit>",
  "startup_delay_seconds": <0, 15, 30...>
}
```

Dispatch N flows (one per worker), staggered by 15s each:

```bash
DEPLOYMENT_ID="<deployment_id>"
for i in $(seq 0 $((worker_count - 1))); do
  STARTUP_DELAY=$((i * 15))
  PARAMS='<json with startup_delay_seconds set to $STARTUP_DELAY>'
  curl -s -X POST \
    "https://prompt-extractor-prefect.fly.dev/api/deployments/${DEPLOYMENT_ID}/create_flow_run" \
    -H "Content-Type: application/json" \
    -d "{\"parameters\": $PARAMS}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('Worker', $((i+1)), '— flow_run_id:', d.get('id'), 'state:', d.get('state',{}).get('type'))"
done
```

Collect all dispatched flow run IDs.

---

### Step 8 — Start monitoring loop

Report the dispatched flows, then call ScheduleWakeup to begin the 5-minute polling loop.

Determine `app_name` from the type/region table above.

Encode state into the loop prompt:
```
/dispatch --monitor batch_id=<batch_id> flow_runs=<id1>,<id2>,<id3> machines=<started_machine_ids> worker_count=<N> extraction_type=<type> deployment_id=<deployment_id> app=<fly_app_name>
```

Call:
```
ScheduleWakeup(
  delaySeconds=300,
  reason="First check 5min after dispatching batch <batch_id>",
  prompt="/dispatch --monitor batch_id=... flow_runs=... machines=... worker_count=N extraction_type=... deployment_id=... app=..."
)
```

---

## Machine ID reference

### prompt-extractor-uk (GPT UK browser)
| Machine ID | State (check live) |
|---|---|
| 28716d6cd76698 | — |
| 784920df1490e8 | — |
| 0805614bd911d8 | — |
| d896d6da5d3938 | — |
| 080d0d5c197d98 | — |
| 891244c62419e8 | — |

### prompt-extractor-google-us (Google AI Overview / Mode)
| Machine ID |
|---|
| 7814727a3d3d78 |
| e820120f39d208 |
| 48e1342ae50278 |
| 7845030b64de18 |

### prompt-extractor-perplexity-uk
| Machine ID |
|---|
| 32870297b46948 |

### prompt-extractor-us (GPT US browser)
| Machine ID |
|---|
| d89590ebed9308 |
| 781e5d1c6e9128 |
| 8d4e06ced91468 |
| 822e94c79651d8 |
| 78452e3b292208 |
| e829397bdd1038 |
| d8d3160b35de68 |
| 6837ee3ce30758 |
| 2869142b977568 |
| d8927e5c775e58 |
| d8de560a452958 |

---

## Notes

- Always use the Prefect REST API directly (not CLI) — SSH port 22 is sometimes blocked
- Prefect API base: `https://prompt-extractor-prefect.fly.dev/api`
- Never commit credentials or API keys
- `flyctl` is available in PATH
- The `mcp__supabase__execute_sql` tool is available for DB queries
- When a flow is FAILED due to Cloudflare: replace it automatically (one retry). If the replacement also fails on Cloudflare within the same session, alert the user and skip that slot.
