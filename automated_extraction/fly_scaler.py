"""
Fly.io Machines API client for dynamically scaling Prefect worker machines.

Scale-up cycle
--------------
1. List all machines → separate originals (no FLY_CLONE_LABEL) from clones
2. Start any stopped originals up to the target count
3. Clone additional machines beyond the original pool — stripping volume mounts
   so each clone gets an ephemeral /data directory
4. Clones are tagged with FLY_CLONE_LABEL env var so scale-down can identify
   and destroy them without touching the original permanent machines
5. Update Prefect work-pool concurrency limit to match the live machine count

Scale-down cycle
----------------
1. Destroy all cloned machines (identified by FLY_CLONE_LABEL)
2. Stop original machines above keep_count
3. Reset Prefect work-pool concurrency limit

Environment variables
---------------------
FLY_API_TOKEN         — Fly.io bearer token (required for all Machines API calls)
FLY_APP_NAME_UK       — UK app name, default "prompt-extractor-uk"
FLY_APP_NAME_US       — US app name, default "prompt-extractor-us"
PREFECT_API_URL       — Prefect server URL (already set on machines)

Usage
-----
  from automated_extraction.fly_scaler import scale_up, scale_down

  result = scale_up("prompt-extractor-uk", target_count=8, prefect_api_url="...", work_pool="prompt-extraction-uk")
  ...
  scale_down("prompt-extractor-uk", keep_count=1, prefect_api_url="...", work_pool="prompt-extraction-uk")
"""

from __future__ import annotations

import copy
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

# ── Fly.io Machines REST API ────────────────────────────────────────────────
FLY_API_BASE = "https://api.machines.dev/v1"

# Env-var key injected into every cloned machine so we can identify it later.
_CLONE_LABEL_ENV_KEY = "FLY_CLONE_LABEL"

# Region map used when creating new machines (default per app name).
_APP_REGION: dict[str, str] = {
    "prompt-extractor-uk": "lhr",
    "prompt-extractor-us": "iad",
    "prompt-extractor-claude-uk": "lhr",
    "prompt-extractor-claude-us": "iad",
    "prompt-extractor-perplexity-uk": "lhr",
    "prompt-extractor-perplexity-us": "iad",
    "gpt-extractor-uk": "lhr",
    "gpt-extractor-us": "iad",
    "prompt-extractor-google-us": "iad",
    "prompt-extractor-google-uk": "lhr",
}


# ── Low-level client ─────────────────────────────────────────────────────────


class FlyMachinesClient:
    """Thin synchronous wrapper around the Fly.io Machines REST API."""

    def __init__(self, api_token: str | None = None) -> None:
        token = api_token or os.getenv("FLY_API_TOKEN")
        if not token:
            raise RuntimeError(
                "FLY_API_TOKEN environment variable is not set. "
                "Create a token with `fly auth token` and set it as a Fly secret: "
                "fly secrets set FLY_API_TOKEN=<token> -a <app-name>"
            )
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = 30.0

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=FLY_API_BASE,
            headers=self._headers,
            timeout=self._timeout,
        )

    # ── Machine lifecycle ────────────────────────────────────────────────────

    def list_machines(self, app_name: str) -> list[dict[str, Any]]:
        with self._client() as c:
            resp = c.get(f"/apps/{app_name}/machines")
            resp.raise_for_status()
            return resp.json()

    def get_machine(self, app_name: str, machine_id: str) -> dict[str, Any]:
        with self._client() as c:
            resp = c.get(f"/apps/{app_name}/machines/{machine_id}")
            resp.raise_for_status()
            return resp.json()

    def start_machine(self, app_name: str, machine_id: str) -> None:
        with self._client() as c:
            resp = c.post(f"/apps/{app_name}/machines/{machine_id}/start")
            resp.raise_for_status()
        LOGGER.info("Started machine %s on %s", machine_id, app_name)

    def stop_machine(self, app_name: str, machine_id: str, timeout_sec: int = 30) -> None:
        with self._client() as c:
            resp = c.post(
                f"/apps/{app_name}/machines/{machine_id}/stop",
                json={"timeout": timeout_sec},
            )
            resp.raise_for_status()
        LOGGER.info("Stopped machine %s on %s", machine_id, app_name)

    def destroy_machine(self, app_name: str, machine_id: str) -> None:
        """Stop (if running) then force-delete a machine."""
        try:
            self.stop_machine(app_name, machine_id)
            self._wait_for_state(app_name, machine_id, "stopped", timeout=60)
        except Exception as exc:
            LOGGER.debug("Stop before destroy failed (may already be stopped): %s", exc)
        with self._client() as c:
            resp = c.delete(
                f"/apps/{app_name}/machines/{machine_id}",
                params={"force": "true"},
            )
            resp.raise_for_status()
        LOGGER.info("Destroyed machine %s on %s", machine_id, app_name)

    def clone_machine(
        self,
        app_name: str,
        source_machine_id: str,
        clone_label: str,
        profile_index: int | None = None,
    ) -> dict[str, Any]:
        """
        Clone a machine by copying its config.

        Volume mounts are stripped so each clone gets ephemeral storage
        (Fly.io volumes are single-attach and cannot be shared between machines).
        The FLY_CLONE_LABEL env var is injected so scale-down can identify
        clones vs permanent original machines.

        If profile_index is provided, CHROME_PROFILE_INDEX is injected so the
        entrypoint restores the matching pre-logged-in Chrome profile from
        Supabase Storage on startup.
        """
        source = self.get_machine(app_name, source_machine_id)
        config: dict[str, Any] = copy.deepcopy(source.get("config", {}))

        # ── Strip volume mounts (single-attach — clones can't share volumes) ──
        config.pop("mounts", None)

        # ── Tag the clone ────────────────────────────────────────────────────
        env: dict[str, str] = config.get("env", {}) or {}
        env[_CLONE_LABEL_ENV_KEY] = clone_label
        # Point Chrome profile to ephemeral path since /data is not mounted.
        env.setdefault("CHATGPT_CHROME_USER_DATA_DIR", "/tmp/chrome-profile")  # nosec B108 — ephemeral Chrome profile on cloned machines, not user-controlled
        # Assign a profile snapshot index so the entrypoint can restore the
        # correct pre-logged-in profile from Supabase Storage.
        if profile_index is not None:
            env["CHROME_PROFILE_INDEX"] = str(profile_index)
        config["env"] = env

        region = source.get("region") or _APP_REGION.get(app_name, "lhr")
        body = {
            "name": f"clone-{clone_label}",
            "config": config,
            "region": region,
            "skip_launch": False,  # start immediately
        }

        with self._client() as c:
            resp = c.post(f"/apps/{app_name}/machines", json=body)
            resp.raise_for_status()
            new_machine: dict[str, Any] = resp.json()

        machine_id = new_machine["id"]
        LOGGER.info("Cloned machine %s → %s (label=%s)", source_machine_id, machine_id, clone_label)
        return new_machine

    def _wait_for_state(
        self,
        app_name: str,
        machine_id: str,
        state: str,
        timeout: int = 120,
    ) -> None:
        """Poll until the machine reaches the desired state or timeout expires."""
        deadline = time.time() + timeout
        with self._client() as c:
            while time.time() < deadline:
                try:
                    resp = c.get(
                        f"/apps/{app_name}/machines/{machine_id}/wait",
                        params={"state": state, "timeout": min(30, int(deadline - time.time()))},
                    )
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                time.sleep(3)
        raise TimeoutError(f"Machine {machine_id} did not reach state={state!r} within {timeout}s")

    def wait_for_started(self, app_name: str, machine_id: str, timeout: int = 120) -> None:
        self._wait_for_state(app_name, machine_id, "started", timeout=timeout)


# ── Helper: identify clones ──────────────────────────────────────────────────


def _is_clone(machine: dict[str, Any]) -> bool:
    env = (machine.get("config") or {}).get("env") or {}
    return bool(env.get(_CLONE_LABEL_ENV_KEY))


# ── Prefect work-pool concurrency update ─────────────────────────────────────


def update_work_pool_concurrency(
    prefect_api_url: str,
    pool_name: str,
    limit: int,
) -> None:
    """Set the Prefect work-pool concurrency limit via the REST API."""
    url = f"{prefect_api_url.rstrip('/')}/work_pools/{pool_name}"
    try:
        with httpx.Client(timeout=15.0) as c:
            resp = c.patch(url, json={"concurrency_limit": limit})
            resp.raise_for_status()
        LOGGER.info("Updated work pool %r concurrency limit → %d", pool_name, limit)
    except Exception as exc:
        LOGGER.warning("Failed to update work pool concurrency (non-fatal): %s", exc)


# ── High-level scale functions ────────────────────────────────────────────────


@dataclass
class ScaleResult:
    app_name: str
    target_count: int
    original_started: list[str] = field(default_factory=list)
    clones_created: list[str] = field(default_factory=list)
    already_running: list[str] = field(default_factory=list)
    total_running: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "target_count": self.target_count,
            "original_started": self.original_started,
            "clones_created": self.clones_created,
            "already_running": self.already_running,
            "total_running": self.total_running,
        }


def scale_up(
    app_name: str,
    target_count: int,
    prefect_api_url: str | None = None,
    work_pool: str | None = None,
    *,
    api_token: str | None = None,
    wait_for_workers_seconds: int = 30,
) -> ScaleResult:
    """
    Ensure at least `target_count` machines are running on `app_name`.

    Strategy
    --------
    1. Start any stopped *original* machines (those without FLY_CLONE_LABEL) up
       to the needed count.
    2. Clone additional machines from the first running original if still needed.
    3. Update Prefect work-pool concurrency to match the new machine count.
    4. Optionally wait `wait_for_workers_seconds` for new Prefect workers to connect
       before callers submit flow runs.

    Parameters
    ----------
    app_name          : Fly.io app name (e.g. "prompt-extractor-uk").
    target_count      : Number of machines to have running.
    prefect_api_url   : Prefect API URL — if provided, updates concurrency limit.
    work_pool         : Prefect work pool name — required if prefect_api_url given.
    api_token         : Override FLY_API_TOKEN env var.
    wait_for_workers_seconds : Seconds to sleep after scaling so new workers can
                        register with Prefect before flows are dispatched.
    """
    client = FlyMachinesClient(api_token)
    result = ScaleResult(app_name=app_name, target_count=target_count)

    machines = client.list_machines(app_name)
    active = [m for m in machines if m.get("state") != "destroyed"]

    originals = [m for m in active if not _is_clone(m)]
    already_running = [m for m in active if m.get("state") == "started"]
    result.already_running = [m["id"] for m in already_running]

    LOGGER.info(
        "scale_up: app=%s target=%d running=%d originals=%d clones_existing=%d",
        app_name,
        target_count,
        len(already_running),
        len(originals),
        len([m for m in active if _is_clone(m)]),
    )

    if len(already_running) >= target_count:
        LOGGER.info("Already have %d machines running — no action needed.", len(already_running))
        result.total_running = len(already_running)
        if prefect_api_url and work_pool:
            update_work_pool_concurrency(prefect_api_url, work_pool, len(already_running))
        return result

    needed = target_count - len(already_running)

    # ── Step 1: Start stopped originals ──────────────────────────────────────
    stopped_originals = [m for m in originals if m.get("state") == "stopped"]
    to_start = stopped_originals[:needed]
    for machine in to_start:
        mid = machine["id"]
        LOGGER.info("Starting stopped original machine %s", mid)
        client.start_machine(app_name, mid)
        result.original_started.append(mid)
        needed -= 1

    # ── Step 2: Clone additional machines ─────────────────────────────────────
    if needed > 0:
        # Pick the first running original as clone source.
        running_originals = [m for m in originals if m.get("state") == "started"]
        source = (running_originals or to_start or originals)[0]
        source_id = source["id"]
        ts = int(time.time())
        total_accounts = int(os.getenv("CHROME_PROFILE_TOTAL_ACCOUNTS", "0"))
        for i in range(needed):
            label = f"{ts}-{i}"
            # Assign a profile index if Chrome profile snapshots are configured.
            # Wraps round-robin if there are fewer accounts than clones.
            profile_index = (i % total_accounts) if total_accounts > 0 else None
            LOGGER.info(
                "Cloning machine %s (label=%s, profile_index=%s, %d/%d)",
                source_id,
                label,
                profile_index,
                i + 1,
                needed,
            )
            new_machine = client.clone_machine(app_name, source_id, label, profile_index=profile_index)
            result.clones_created.append(new_machine["id"])

    # ── Wait for new machines/workers to be ready ──────────────────────────
    if result.original_started or result.clones_created:
        newly_added = len(result.original_started) + len(result.clones_created)
        wait_secs = wait_for_workers_seconds if newly_added > 0 else 0
        if wait_secs > 0:
            LOGGER.info(
                "Waiting %ds for %d new Prefect worker(s) to connect…",
                wait_secs,
                newly_added,
            )
            time.sleep(wait_secs)

    result.total_running = len(already_running) + len(result.original_started) + len(result.clones_created)

    # ── Update Prefect concurrency limit ───────────────────────────────────
    if prefect_api_url and work_pool:
        update_work_pool_concurrency(prefect_api_url, work_pool, result.total_running)

    LOGGER.info(
        "scale_up complete: started_originals=%d clones_created=%d total_running=%d",
        len(result.original_started),
        len(result.clones_created),
        result.total_running,
    )
    return result


@dataclass
class ScaleDownResult:
    app_name: str
    keep_count: int
    clones_destroyed: list[str] = field(default_factory=list)
    originals_stopped: list[str] = field(default_factory=list)
    remaining_running: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "keep_count": self.keep_count,
            "clones_destroyed": self.clones_destroyed,
            "originals_stopped": self.originals_stopped,
            "remaining_running": self.remaining_running,
        }


def scale_down(
    app_name: str,
    keep_count: int = 1,
    prefect_api_url: str | None = None,
    work_pool: str | None = None,
    *,
    api_token: str | None = None,
) -> ScaleDownResult:
    """
    Scale down to `keep_count` running machines.

    All clone machines (FLY_CLONE_LABEL set) are destroyed.
    Original machines beyond keep_count are stopped (not destroyed).

    Parameters
    ----------
    app_name      : Fly.io app name.
    keep_count    : Number of original machines to leave running (default 1).
    prefect_api_url : If provided, resets work-pool concurrency to keep_count.
    work_pool     : Prefect work pool name.
    api_token     : Override FLY_API_TOKEN env var.
    """
    client = FlyMachinesClient(api_token)
    result = ScaleDownResult(app_name=app_name, keep_count=keep_count)

    machines = client.list_machines(app_name)
    active = [m for m in machines if m.get("state") != "destroyed"]

    clones = [m for m in active if _is_clone(m)]
    originals = [m for m in active if not _is_clone(m)]
    running_originals = [m for m in originals if m.get("state") == "started"]

    LOGGER.info(
        "scale_down: app=%s keep=%d running_originals=%d clones=%d",
        app_name,
        keep_count,
        len(running_originals),
        len(clones),
    )

    # ── Destroy all clones ─────────────────────────────────────────────────
    for machine in clones:
        mid = machine["id"]
        LOGGER.info("Destroying clone %s", mid)
        try:
            client.destroy_machine(app_name, mid)
            result.clones_destroyed.append(mid)
        except Exception as exc:
            LOGGER.error("Failed to destroy clone %s: %s", mid, exc)

    # ── Stop excess original machines ──────────────────────────────────────
    excess_originals = running_originals[keep_count:]
    for machine in excess_originals:
        mid = machine["id"]
        LOGGER.info("Stopping original machine %s", mid)
        try:
            client.stop_machine(app_name, mid)
            result.originals_stopped.append(mid)
        except Exception as exc:
            LOGGER.error("Failed to stop machine %s: %s", mid, exc)

    result.remaining_running = len(running_originals) - len(result.originals_stopped)

    # ── Reset Prefect concurrency limit ────────────────────────────────────
    if prefect_api_url and work_pool:
        update_work_pool_concurrency(prefect_api_url, work_pool, max(keep_count, 1))

    LOGGER.info(
        "scale_down complete: clones_destroyed=%d originals_stopped=%d remaining=%d",
        len(result.clones_destroyed),
        len(result.originals_stopped),
        result.remaining_running,
    )
    return result


# ── Convenience: resolve app name from region ─────────────────────────────────


def app_name_for_region(region: str) -> str:
    """Return the Fly.io app name for a given region string."""
    env_key = f"FLY_APP_NAME_{region.upper()}"
    return os.getenv(env_key) or f"prompt-extractor-{region.lower()}"
