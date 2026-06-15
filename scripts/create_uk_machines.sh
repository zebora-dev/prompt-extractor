#!/usr/bin/env bash
# create_uk_machines.sh — Provision 9 dedicated UK Fly machines for ChatGPT extraction
#
# One machine per ChatGPT account, each with its own volume.
# The existing machine d8d3744c34e4e8 is assigned profile 3 (frank@theround.com).
# This script creates the remaining 8 machines (profiles 0-2, 4-8).
#
# Usage:
#   cd <worktree>
#   bash scripts/create_uk_machines.sh
#
# Prerequisites:
#   - fly CLI authenticated and targeting the prompt-extractor-uk app
#   - App secrets already set (BRANDSIGHT_*, PREFECT_API_URL, etc.)

set -euo pipefail

APP="prompt-extractor-uk"
REGION="lhr"
# Profiles to create (skip 3 — already on the existing machine d8d3744c34e4e8)
declare -A ACCOUNTS=(
  [0]="dev@theround.com"
  [1]="chris@theround.com"
  [2]="bob@theround.com"
  [4]="info@zebora.io"
  [5]="dev@zebora.io"
  [6]="data@zebora.io"
  [7]="rob@zebora.io"
  [8]="john@zebora.io"
)

echo "==> Creating 8 new Fly machines for app ${APP} in ${REGION} ..."
echo ""

for index in 0 1 2 4 5 6 7 8; do
  email="${ACCOUNTS[$index]}"
  volume_name="prompt_extractor_data_uk_${index}"

  echo "── Profile ${index}: ${email} ─────────────────────────────────────────"

  # Create a dedicated volume for this machine
  echo "   Creating volume ${volume_name} ..."
  fly volumes create "${volume_name}" \
    --app "${APP}" \
    --region "${REGION}" \
    --size 5 \
    --yes \
    2>&1 | tail -3

  # Capture the volume ID from the list
  VOLUME_ID=$(fly volumes list --app "${APP}" --json 2>/dev/null \
    | python3 -c "
import json, sys
vols = json.load(sys.stdin)
matches = [v for v in vols if v.get('name') == '${volume_name}' and v.get('region') == '${REGION}' and not v.get('attached_machine_id')]
if matches:
    print(matches[-1]['id'])
" 2>/dev/null || true)

  if [[ -z "${VOLUME_ID}" ]]; then
    echo "   WARNING: Could not determine volume ID for ${volume_name}. Skipping machine creation."
    continue
  fi

  echo "   Volume ID: ${VOLUME_ID}"
  echo "   Creating machine for profile ${index} (${email}) ..."

  # Create the machine — it starts stopped so you can log in via VNC before enabling
  MACHINE_JSON=$(fly machine run \
    --app "${APP}" \
    --region "${REGION}" \
    --image "registry.fly.io/${APP}:latest" \
    --env "CHROME_PROFILE_INDEX=${index}" \
    --env "CHATGPT_LOGIN_EMAIL=${email}" \
    --volume "${VOLUME_ID}:/data" \
    --vm-cpus 1 \
    --vm-cpu-kind performance \
    --vm-memory 2048 \
    --no-public-ips \
    --json \
    2>&1)

  MACHINE_ID=$(echo "${MACHINE_JSON}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id','?'))" 2>/dev/null || echo "?")
  echo "   Machine created: ${MACHINE_ID} (profile=${index}, email=${email})"
  echo "   Next: log into VNC at https://fly.io/apps/${APP}/machines/${MACHINE_ID}"
  echo ""
done

echo "==> Done. Assign account on existing machine d8d3744c34e4e8 (profile 3):"
echo "    fly machine update d8d3744c34e4e8 -a ${APP} --env CHROME_PROFILE_INDEX=3 --env CHATGPT_LOGIN_EMAIL=frank@theround.com"
echo ""
echo "==> Stop all machines when idle to save cost:"
echo "    fly machine list -a ${APP} --json | python3 -c \"import json,sys; [print(m['id']) for m in json.load(sys.stdin)]\" | xargs -I{} fly machine stop {} -a ${APP}"
