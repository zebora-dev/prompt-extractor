#!/usr/bin/env bash
set -euo pipefail

if [[ "${PROMPT_EXTRACTOR_VNC:-false}" == "true" ]]; then
  Xvfb "${DISPLAY}" -screen 0 "${VNC_SCREEN:-1920x1080x24}" -ac +extension GLX +render -noreset &
  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
  echo "noVNC is available on port 6080. Open http://<server>:6080/vnc.html"
  exec "$@"
fi

exec xvfb-run -a --server-args="-screen 0 ${VNC_SCREEN:-1920x1080x24} -ac +extension GLX +render -noreset" "$@"
