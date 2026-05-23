---
name: remote-control
description: Remote control for the BrandSight UK extraction workers and Prefect dispatches. Use for starting/stopping workers, triggering GPT extraction dispatches, and checking batch prompt status.
argument-hint: [workers on|off|status] | [dispatch <batch_id>] | [status <batch_id>] | [batch <batch_id>]
disable-model-invocation: true
allowed-tools: Bash
---

# BrandSight UK Worker Remote Control

You are controlling the BrandSight UK extraction infrastructure. Execute the requested operation using the constants below.

## Operation requested
$ARGUMENTS

---

## Constants

### Fly.io
- **App:** `prompt-extractor-uk`
- **CLI prefix:** `fly machine <start|stop|update> <id> -a prompt-extractor-uk`

### UK Machines (all 9)
| Machine ID       | Account                |
|------------------|------------------------|
| e829420a634578   | dev@theround.com       |
| 7849237b673708   | chris@theround.com     |
| e82949df4390d8   | bob@theround.com       |
| d8d3744c34e4e8   | frank@theround.com     |
| 7849237b673208   | info@zebora.io         |
| e829491b6d4268   | dev@zebora.io          |
| d896d6da5d3938   | data@zebora.io         |
| 0805610f32d018   | rob@zebora.io          |
| 18592e4a677678   | john@zebora.io         |

### Prefect
- **API URL:** `https://prompt-extractor-prefect.fly.dev/api`
- **Work pool:** `prompt-extraction-uk`
- **Dispatch deployment:** `dispatch-extraction/dispatch-extraction-uk`
- **Batch deployment:** `chatgpt-extraction-batch/chatgpt-extraction-batch-uk`
- **Always prefix Prefect CLI with:** `PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api`

### Batch Status API
- **Endpoint:** `GET https://workflow.zebora.io/api/batches/{batch_id}/status/outputs`
- **Script:** `./scripts/batch_status.sh <batch_id>`

---

## Operations

### `workers on`
Start all 9 machines, then wait until all 9 Prefect workers are ONLINE before confirming.

```bash
# Start all machines
for id in e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678; do
  echo -n "Starting $id ... "
  fly machine start $id -a prompt-extractor-uk 2>&1 | tail -1
done

# Wait for all 9 to connect to Prefect
until [ $(curl -s -X POST "https://prompt-extractor-prefect.fly.dev/api/work_pools/prompt-extraction-uk/workers/filter" \
  -H "Content-Type: application/json" -d '{}' | python3 -c "
import json,sys; w=json.loads(sys.stdin.read()); print(len([x for x in w if x.get('status')=='ONLINE']))
") -ge 9 ]; do sleep 5; done && echo "All 9 workers online ✅"
```

### `workers off`
Stop all 9 machines.

```bash
for id in e829420a634578 7849237b673708 e82949df4390d8 d8d3744c34e4e8 7849237b673208 e829491b6d4268 d896d6da5d3938 0805610f32d018 18592e4a677678; do
  echo -n "Stopping $id ... "
  fly machine stop $id -a prompt-extractor-uk 2>&1 | tail -1
done
```

### `workers status`
Show machine states and count of online Prefect workers.

```bash
fly machines list -a prompt-extractor-uk --json | jq -r '.[] | [.id, .state, .config.env.CHATGPT_LOGIN_EMAIL] | @tsv' | sort -t$'\t' -k3

curl -s -X POST "https://prompt-extractor-prefect.fly.dev/api/work_pools/prompt-extraction-uk/workers/filter" \
  -H "Content-Type: application/json" -d '{}' | python3 -c "
import json,sys
w=json.loads(sys.stdin.read())
online=[x for x in w if x.get('status')=='ONLINE']
print(f'Prefect workers online: {len(online)}/9')
"
```

### `dispatch <batch_id> [worker_count]`
Trigger a GPT extraction dispatch across all workers (default 9). Always uses `capture_products=true` and `capture_entities=true`.

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api prefect deployment run \
  'dispatch-extraction/dispatch-extraction-uk' \
  --param batch_id=<batch_id> \
  --param extraction_type=chatgpt \
  --param worker_count=9 \
  --param region=uk \
  --param capture_products=true \
  --param capture_entities=true
```

### `batch <batch_id>`
Run a single chatgpt-extraction-batch (one worker, sequential, runs until exhausted).

```bash
PREFECT_API_URL=https://prompt-extractor-prefect.fly.dev/api prefect deployment run \
  'chatgpt-extraction-batch/chatgpt-extraction-batch-uk' \
  --param batch_id=<batch_id> \
  --param capture_products=true \
  --param capture_entities=true
```

### `status <batch_id>`
Check how many prompts are complete vs remaining for a batch.

```bash
./scripts/batch_status.sh <batch_id>
```

Or directly:
```bash
curl -s "https://workflow.zebora.io/api/batches/<batch_id>/status/outputs" | python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
prompts=d.get('prompts_count',0); outputs=d.get('outputs_count',0)
print(f'Status:    {d.get(\"status\")}')
print(f'Prompts:   {prompts}')
print(f'Outputs:   {outputs}')
print(f'Remaining: {prompts - outputs}')
for m in (d.get('llm') or []):
    print(f'  {m[\"llm_model\"]}: {m[\"outputs_count\"]} outputs ({m[\"status\"]})')
"
```

---

## Important notes
- Always deploy from the **worktree** (`feat/google` branch), not the main repo directory
- `fly deploy` will always error on volume mismatch — that's expected; use `fly machine update --image <tag>` per machine
- Prefect deployments need `working_dir=/app` — if they break, PATCH via the API
- Prefect CLI commands must be prefixed with `PREFECT_API_URL=...`
