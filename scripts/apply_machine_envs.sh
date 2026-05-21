#!/usr/bin/env bash
# apply_machine_envs.sh — Re-apply per-machine profile env vars after a fly deploy.
#
# `fly deploy` wipes per-machine env vars set via `fly machine update --env`.
# Run this script after every deploy to restore CHROME_PROFILE_INDEX and
# CHATGPT_LOGIN_EMAIL on each machine.
#
# Usage:
#   bash scripts/apply_machine_envs.sh

set -euo pipefail
APP="prompt-extractor-uk"

echo "Re-applying per-machine profile env vars for app ${APP} ..."

fly machine update d8d3744c34e4e8 -a "${APP}" --env CHROME_PROFILE_INDEX=3 --env CHATGPT_LOGIN_EMAIL=frank@theround.com --yes
fly machine update 2860604f5009d8 -a "${APP}" --env CHROME_PROFILE_INDEX=0 --env CHATGPT_LOGIN_EMAIL=dev@theround.com --yes
fly machine update 1859296b6337e8 -a "${APP}" --env CHROME_PROFILE_INDEX=1 --env CHATGPT_LOGIN_EMAIL=chris@theround.com --yes
fly machine update 0805656b34dd78 -a "${APP}" --env CHROME_PROFILE_INDEX=2 --env CHATGPT_LOGIN_EMAIL=bob@theround.com --yes
fly machine update 6839393f59e098 -a "${APP}" --env CHROME_PROFILE_INDEX=4 --env CHATGPT_LOGIN_EMAIL=info@zebora.io --yes
fly machine update d8d3736f377028 -a "${APP}" --env CHROME_PROFILE_INDEX=5 --env CHATGPT_LOGIN_EMAIL=dev@zebora.io --yes
fly machine update 48e4545a062478 -a "${APP}" --env CHROME_PROFILE_INDEX=6 --env CHATGPT_LOGIN_EMAIL=data@zebora.io --yes
fly machine update 48e2363f2904d8 -a "${APP}" --env CHROME_PROFILE_INDEX=7 --env CHATGPT_LOGIN_EMAIL=rob@zebora.io --yes
fly machine update 2860605c36d928 -a "${APP}" --env CHROME_PROFILE_INDEX=8 --env CHATGPT_LOGIN_EMAIL=john@zebora.io --yes

echo "Done."
