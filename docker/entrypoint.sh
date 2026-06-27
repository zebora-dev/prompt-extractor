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

  exec "$@"
fi

# Non-VNC path: xvfb-run manages the display; persistent Chrome not supported
# in this mode (no stable display reference before exec).
exec xvfb-run -a --server-args="-screen 0 ${VNC_SCREEN:-1920x1080x24} -ac +extension GLX +render -noreset" "$@"
