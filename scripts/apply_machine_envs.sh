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

fly machine update 0805626fe21498 -a "${APP}" --env CHROME_PROFILE_INDEX=0 --env CHATGPT_LOGIN_EMAIL=dev@theround.com --yes
fly machine update 683932eae9d968 -a "${APP}" --env CHROME_PROFILE_INDEX=1 --env CHATGPT_LOGIN_EMAIL=chris@theround.com --yes
fly machine update 784920df1490e8 -a "${APP}" --env CHROME_PROFILE_INDEX=2 --env CHATGPT_LOGIN_EMAIL=bob@theround.com --yes
fly machine update d8d3744c34e4e8 -a "${APP}" --env CHROME_PROFILE_INDEX=3 --env CHATGPT_LOGIN_EMAIL=frank@theround.com --yes
fly machine update 7849237b673208 -a "${APP}" --env CHROME_PROFILE_INDEX=4 --env CHATGPT_LOGIN_EMAIL=info@zebora.io --yes
fly machine update 0805614bd911d8 -a "${APP}" --env CHROME_PROFILE_INDEX=5 --env CHATGPT_LOGIN_EMAIL=dev@zebora.io --yes
fly machine update d896d6da5d3938 -a "${APP}" --env CHROME_PROFILE_INDEX=6 --env CHATGPT_LOGIN_EMAIL=data@zebora.io --yes
fly machine update 48e4527fed62d8 -a "${APP}" --env CHROME_PROFILE_INDEX=7 --env CHATGPT_LOGIN_EMAIL=rob@zebora.io --yes
fly machine update 865130be035738 -a "${APP}" --env CHROME_PROFILE_INDEX=8 --env CHATGPT_LOGIN_EMAIL=john@zebora.io --yes

echo "Done."

APP_US="prompt-extractor-us"
echo "Re-applying per-machine profile env vars for app ${APP_US} ..."

fly machine update d8d3160b35de68 -a "${APP_US}" --env CHROME_PROFILE_INDEX=0 --env CHATGPT_LOGIN_EMAIL=dev@theround.com --yes
fly machine update e829397bdd1038 -a "${APP_US}" --env CHROME_PROFILE_INDEX=1 --env CHATGPT_LOGIN_EMAIL=chris@theround.com --yes
fly machine update d8927e5c775e58 -a "${APP_US}" --env CHROME_PROFILE_INDEX=2 --env CHATGPT_LOGIN_EMAIL=bob@theround.com --yes
fly machine update 78452e3b292208 -a "${APP_US}" --env CHROME_PROFILE_INDEX=3 --env CHATGPT_LOGIN_EMAIL=frank@theround.com --yes
fly machine update 822e94c79651d8 -a "${APP_US}" --env CHROME_PROFILE_INDEX=4 --env CHATGPT_LOGIN_EMAIL=info@zebora.io --yes
fly machine update 781e5d1c6e9128 -a "${APP_US}" --env CHROME_PROFILE_INDEX=5 --env CHATGPT_LOGIN_EMAIL=dev@zebora.io --yes
fly machine update 6837ee3ce30758 -a "${APP_US}" --env CHROME_PROFILE_INDEX=6 --env CHATGPT_LOGIN_EMAIL=data@zebora.io --yes
fly machine update d89590ebed9308 -a "${APP_US}" --env CHROME_PROFILE_INDEX=7 --env CHATGPT_LOGIN_EMAIL=rob@zebora.io --yes
fly machine update 8d4e06ced91468 -a "${APP_US}" --env CHROME_PROFILE_INDEX=8 --env CHATGPT_LOGIN_EMAIL=john@zebora.io --yes

echo "Done."
