"""
Slack notification helpers for BrandSight extraction workers.

Sends alerting messages to a configured Slack channel when operator
intervention is required (e.g. Cloudflare challenge detected).

Usage
-----
Notifications are fire-and-forget.  If SLACK_BOT_TOKEN is not set the module
is a no-op — local dev and test runs are unaffected.

Required env vars
-----------------
SLACK_BOT_TOKEN   — Slack bot OAuth token (xoxb-...)
SLACK_CHANNEL_ID  — Target channel ID, e.g. C0ABPV27S58 (#dev)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api/chat.postMessage"
_DEFAULT_CHANNEL = "C0ABPV27S58"  # #dev — zeboraworkspace.slack.com


def _bot_token() -> str | None:
    return (os.getenv("SLACK_BOT_TOKEN") or "").strip() or None


def _channel_id() -> str:
    return (os.getenv("SLACK_CHANNEL_ID") or _DEFAULT_CHANNEL).strip()


def _machine_context() -> dict[str, str]:
    """Collect Fly.io machine context from env vars (empty strings locally)."""
    return {
        "machine_id": os.getenv("FLY_MACHINE_ID", "local"),
        "app_name": os.getenv("FLY_APP_NAME", "local"),
        "region": os.getenv("FLY_REGION", ""),
        "login_email": os.getenv("CHATGPT_LOGIN_EMAIL", "unknown"),
    }


def _post(blocks: list[dict[str, Any]], text: str) -> None:
    """
    POST a Block Kit message to Slack.  Logs a warning if the call fails or
    if the token is not configured — never raises.
    """
    token = _bot_token()
    if not token:
        LOGGER.debug("SLACK_BOT_TOKEN not set — skipping Slack notification.")
        return

    try:
        resp = requests.post(
            SLACK_API_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": _channel_id(), "text": text, "blocks": blocks},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            LOGGER.warning("Slack notification failed: %s", data.get("error", data))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Slack notification error (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────


def notify_cloudflare_challenge(
    *,
    signals: list[str],
    title: str,
    url: str,
    context: str,
    prefect_run_url: str | None = None,
) -> None:
    """
    Send a Slack alert when a Cloudflare 'Are you human?' challenge is detected.

    Called once per challenge encounter (not on every 30-second reminder log).
    """
    ctx = _machine_context()
    machine_id = ctx["machine_id"]
    login_email = ctx["login_email"]
    region = ctx["region"]
    app = ctx["app_name"]

    # vnc-redirect.html sets fly_instance_id cookie via JS so Fly's LB routes
    # the subsequent /vnc.html request directly to this machine.
    vnc_url = f"https://{app}.fly.dev/vnc-redirect.html?machine={machine_id}" if app != "local" else None

    summary = f"`{machine_id}` · {region or 'unknown'} · `{login_email}`"
    vnc_line = f"<{vnc_url}|VNC in to machine `{machine_id}`>" if vnc_url else "VNC not available locally."

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":warning: *Cloudflare challenge* — {summary}\n{vnc_line}",
            },
        },
    ]

    if prefect_run_url:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Prefect run*\n<{prefect_run_url}|View logs>"},
            }
        )

    blocks.append({"type": "divider"})

    fallback_text = (
        f"⚠️ Cloudflare challenge on machine {machine_id} ({login_email}) during {context}. VNC in to resolve."
    )
    _post(blocks, fallback_text)


def notify_cloudflare_cleared(
    *,
    elapsed_seconds: int,
    context: str,
) -> None:
    """
    Send a Slack message when a Cloudflare challenge is cleared and the run resumes.
    """
    ctx = _machine_context()
    machine_id = ctx["machine_id"]
    login_email = ctx["login_email"]

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *Cloudflare challenge cleared* on machine `{machine_id}` "
                    f"(`{login_email}`) after {elapsed_seconds}s — run resuming. "
                    f"Context: `{context}`"
                ),
            },
        }
    ]
    _post(blocks, f"✅ Cloudflare challenge cleared on {machine_id} ({login_email}) — run resuming.")
