#!/usr/bin/env bash
# Batch Prompt Status
# Usage: ./scripts/batch_status.sh <batch_id> [brand_id]
#
# Calls the Brand Score Pipeline API to check how many prompts/outputs
# are complete vs remaining for a given batch.
#
# API docs: https://workflow.zebora.io/docs#/batch-status/get_batch_status_outputs_api_batches__batch_id__status_outputs_get

set -euo pipefail

BATCH_ID="${1:?Usage: $0 <batch_id> [brand_id]}"
BRAND_ID="${2:-}"
BASE_URL="https://workflow.zebora.io"

URL="${BASE_URL}/api/batches/${BATCH_ID}/status/outputs"
if [[ -n "$BRAND_ID" ]]; then
  URL="${URL}?brand_id=${BRAND_ID}"
fi

echo "Checking batch status for: ${BATCH_ID}"
echo "URL: ${URL}"
echo ""

curl -s "$URL" | python3 -c "
import json, sys

d = json.loads(sys.stdin.read())

status       = d.get('status', '?')
message      = d.get('message', '')
prompts      = d.get('prompts_count', 0)
outputs      = d.get('outputs_count', 0)
remaining    = prompts - outputs

icon = '✅' if status == 'ok' else ('⚠️' if status == 'incomplete' else '❌')

print(f'{icon}  Status:         {status}')
print(f'   Message:        {message}')
print(f'   Prompts (total): {prompts}')
print(f'   Outputs (done):  {outputs}')
print(f'   Remaining:       {remaining}')
print()

llm = d.get('llm') or []
if llm:
    print('Per-model breakdown:')
    for m in llm:
        mstatus = '✅' if m.get('status') == 'ok' else '⚠️'
        print(f'  {mstatus} {m[\"llm_model\"]:<25} outputs: {m[\"outputs_count\"]}')
    print()

missing = d.get('missing_llm_models') or []
if missing:
    print('Missing models:', ', '.join(missing))
"
