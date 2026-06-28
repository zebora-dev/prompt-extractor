#!/usr/bin/env bash
set -euo pipefail

PROFILE_DEST="${CHATGPT_CHROME_USER_DATA_DIR:-/data/chrome-profile}"
MACHINE_ID="${FLY_MACHINE_ID:-local}"

# ── Account pool mode ─────────────────────────────────────────────────────────
# When ACCOUNT_POOL_ENABLED=true (set in fly-gpt-uk.yaml) the worker dynamically
# claims a Chrome profile from the extraction_accounts pool at startup, downloads
# it from Tigris, and releases + re-uploads it on exit via an EXIT trap.
# When false/unset the legacy volume-based flow is used unchanged.
PROFILE_INDEX=""
if [[ "${ACCOUNT_POOL_ENABLED:-false}" == "true" ]]; then
  echo "[entrypoint] Account pool enabled — claiming account …"

  # acquire outputs: "<index> <email>"
  ACQUIRE_OUTPUT=$(python -m automated_extraction.profile_manager acquire \
    --worker-id "${MACHINE_ID}" --dest "${PROFILE_DEST}")
  PROFILE_INDEX=$(echo "${ACQUIRE_OUTPUT}" | awk '{print $1}')
  export CHATGPT_LOGIN_EMAIL
  CHATGPT_LOGIN_EMAIL=$(echo "${ACQUIRE_OUTPUT}" | awk '{print $2}')

  echo "[entrypoint] Claimed profile ${PROFILE_INDEX} (${CHATGPT_LOGIN_EMAIL})"

  # Register cleanup: upload profile + release lock on any exit (SIGTERM, error, normal).
  # Hard kills (SIGKILL) are covered by the 4-hour lock_expires_at TTL.
  _release_profile() {
    echo "[entrypoint] EXIT trap — saving and releasing profile ${PROFILE_INDEX} …"
    python -m automated_extraction.profile_manager save-and-release \
      --index "${PROFILE_INDEX}" \
      --worker-id "${MACHINE_ID}" \
      --dir "${PROFILE_DEST}" \
      || true  # never block shutdown
  }
  trap _release_profile EXIT
else
  # ── Legacy volume-based flow ─────────────────────────────────────────────────
  echo "[entrypoint] Chrome profile dir: ${PROFILE_DEST}"
  if [[ -d "${PROFILE_DEST}" ]]; then
    echo "[entrypoint] Profile directory exists — using existing session."
  else
    echo "[entrypoint] Profile directory not found — Chrome will create a fresh one on first login."
  fi
fi

# Remove any stale Chrome lock files left over from a previous abrupt stop.
rm -f \
  "${PROFILE_DEST}/SingletonLock" \
  "${PROFILE_DEST}/SingletonSocket" \
  "${PROFILE_DEST}/SingletonCookie" \
  2>/dev/null || true

# ── _start_persistent_chrome helper ──────────────────────────────────────────
# Called after Xvfb is confirmed running so Chrome gets a real display window.
_start_one_chrome() {
  DISPLAY=:99 google-chrome \
    --user-data-dir="${PROFILE_DEST}" \
    --remote-debugging-port=9222 \
    --no-first-run \
    --no-default-browser-check \
    --no-sandbox \
    --disable-session-crashed-bubble \
    --no-restore-last-session \
    https://chatgpt.com \
    >>/tmp/chrome-persistent.log 2>&1 &
  echo $!
}

_start_persistent_chrome() {
  if [[ "${CHATGPT_PERSISTENT_CHROME:-false}" == "true" ]]; then
    echo "[entrypoint] Starting persistent Chrome (profile: ${PROFILE_DEST}) …"
    CHROME_PID=$(_start_one_chrome)
    echo "[entrypoint] Persistent Chrome started (PID ${CHROME_PID}) — waiting for it to be ready …"
    sleep 6
    echo "[entrypoint] Persistent Chrome ready."

    # Watchdog: restart Chrome if it exits unexpectedly.
    (
      while true; do
        if ! kill -0 "${CHROME_PID}" 2>/dev/null; then
          echo "[entrypoint] Persistent Chrome (PID ${CHROME_PID}) exited — restarting …"
          sleep 2
          CHROME_PID=$(_start_one_chrome)
          echo "[entrypoint] Persistent Chrome restarted (PID ${CHROME_PID})."
          sleep 6
        fi
        sleep 5
      done
    ) &
  fi
}

# ── Display / VNC ─────────────────────────────────────────────────────────────
if [[ "${PROMPT_EXTRACTOR_VNC:-false}" == "true" ]]; then
  Xvfb "${DISPLAY}" -screen 0 "${VNC_SCREEN:-1920x1080x24}" -ac +extension GLX +render -noreset &
  sleep 2   # give Xvfb a moment to initialise before Chrome tries to connect
  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport 5900 -loop >/tmp/x11vnc.log 2>&1 &

  # websockify serves noVNC on port 6081; nginx fronts it on port 6080 and
  # handles Fly-Replay routing so /vnc/<machine_id>/... always reaches the
  # correct worker regardless of which machine the LB initially picks.
  websockify --web=/usr/share/novnc/ 6081 localhost:5900 >/tmp/novnc.log 2>&1 &

  MACHINE_ID="${FLY_MACHINE_ID:-local-dev}"
  envsubst '${FLY_MACHINE_ID}' \
    < /etc/nginx/conf.d/vnc.conf.template \
    > /etc/nginx/conf.d/default.conf
  nginx
  echo "noVNC available via nginx on port 6080 — direct link: http://<server>:6080/vnc/${MACHINE_ID}/vnc.html"

  # Xvfb is now running — safe to start persistent Chrome with a real window
  _start_persistent_chrome

  if [[ "${ACCOUNT_POOL_ENABLED:-false}" == "true" ]]; then
    # Keep bash alive as PID 1 so the EXIT trap fires on SIGTERM.
    # exec would replace bash, losing the trap entirely.
    "$@" &
    WORKER_PID=$!
    # Forward SIGTERM/SIGINT to the worker so it shuts down gracefully.
    trap 'kill -TERM $WORKER_PID 2>/dev/null || true' SIGTERM SIGINT
    wait $WORKER_PID || true
    # EXIT trap (_release_profile) runs here automatically.
  else
    exec "$@"
  fi
  exit 0
fi

# Non-VNC path: xvfb-run manages the display; persistent Chrome not supported
# in this mode (no stable display reference before exec).
if [[ "${ACCOUNT_POOL_ENABLED:-false}" == "true" ]]; then
  xvfb-run -a --server-args="-screen 0 ${VNC_SCREEN:-1920x1080x24} -ac +extension GLX +render -noreset" "$@" &
  WORKER_PID=$!
  trap 'kill -TERM $WORKER_PID 2>/dev/null || true' SIGTERM SIGINT
  wait $WORKER_PID || true
else
  exec xvfb-run -a --server-args="-screen 0 ${VNC_SCREEN:-1920x1080x24} -ac +extension GLX +render -noreset" "$@"
fi
