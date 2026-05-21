#!/usr/bin/env bash
set -euo pipefail

# ── Chrome profile location ───────────────────────────────────────────────────
# The Chrome profile lives permanently on the Fly volume mounted at /data.
# CHROME_PROFILE_INDEX and CHATGPT_LOGIN_EMAIL are set as per-machine env vars
# via `fly machine update --env` — one account per machine, no locking needed.
PROFILE_DEST="${CHATGPT_CHROME_USER_DATA_DIR:-/data/chrome-profile}"

echo "[entrypoint] Chrome profile dir: ${PROFILE_DEST}"
if [[ -d "${PROFILE_DEST}" ]]; then
  echo "[entrypoint] Profile directory exists — using existing session."
else
  echo "[entrypoint] Profile directory not found — Chrome will create a fresh one on first login."
fi

# ── _start_persistent_chrome helper ──────────────────────────────────────────
# Called after Xvfb is confirmed running so Chrome gets a real display window.
_start_persistent_chrome() {
  if [[ "${CHATGPT_PERSISTENT_CHROME:-false}" == "true" ]]; then
    echo "[entrypoint] Starting persistent Chrome (profile: ${PROFILE_DEST}) …"
    DISPLAY=:99 google-chrome \
      --user-data-dir="${PROFILE_DEST}" \
      --remote-debugging-port=9222 \
      --no-first-run \
      --no-default-browser-check \
      --no-sandbox \
      --disable-session-crashed-bubble \
      --no-restore-last-session \
      https://chatgpt.com \
      >/tmp/chrome-persistent.log 2>&1 &
    CHROME_PID=$!
    echo "[entrypoint] Persistent Chrome started (PID ${CHROME_PID}) — waiting for it to be ready …"
    sleep 6
    echo "[entrypoint] Persistent Chrome ready."
  fi
}

# ── Display / VNC ─────────────────────────────────────────────────────────────
if [[ "${PROMPT_EXTRACTOR_VNC:-false}" == "true" ]]; then
  Xvfb "${DISPLAY}" -screen 0 "${VNC_SCREEN:-1920x1080x24}" -ac +extension GLX +render -noreset &
  sleep 2   # give Xvfb a moment to initialise before Chrome tries to connect
  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
  echo "noVNC is available on port 6080. Open http://<server>:6080/vnc.html"

  # Xvfb is now running — safe to start persistent Chrome with a real window
  _start_persistent_chrome

  exec "$@"
fi

# Non-VNC path: xvfb-run manages the display; persistent Chrome not supported
# in this mode (no stable display reference before exec).
exec xvfb-run -a --server-args="-screen 0 ${VNC_SCREEN:-1920x1080x24} -ac +extension GLX +render -noreset" "$@"
