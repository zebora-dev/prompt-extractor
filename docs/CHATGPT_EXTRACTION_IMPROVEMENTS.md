# ChatGPT Extraction — Reliability Improvements

Implemented after analysis of batch `de54724b` (614 prompts × 2 models), which revealed
a set of systematic issues: duplicate outputs, missed prompts, silent model downgrades,
and accounts running out of rate-limit budget without detection.

---

## Problems Identified

### 1. Duplicate outputs
614 prompts required both `gpt-5-5` and `gpt-5-3-mini` outputs. After the batch "completed":
- `gpt-5-3-mini`: 1106 total outputs for only 598 unique prompts (508 duplicates)
- `gpt-5-5`: 640 total outputs for only 530 unique prompts (110 duplicates)

**Root cause:** The prompt claim TTL was 5 minutes. ChatGPT responses during Cloudflare
challenges or slow sessions routinely take longer than 5 minutes. When the claim expired,
another worker picked up the same prompt and both workers saved outputs — creating duplicates.

**Fix:** Claim TTL increased from 5 → 20 minutes (`ttl_minutes=20` in `try_claim_prompt`).

### 2. Silent model downgrade (gpt-5-5 → gpt-5-3-mini)
Free/standard ChatGPT accounts serve `gpt-5-5` for the first several prompts after a period
of inactivity, then silently downgrade to `gpt-5-3-mini` as the session continues. The
extractor was saving these degraded outputs as if they were valid `gpt-5-5` captures.

Additionally, ChatGPT may show an "Upgrade for better performance" prompt when the account
is running on the lower model — this was not detected.

**Fix:**
- After each capture, compare the returned `llm_model` against the expected model from
  `required_models`. If a downgrade is detected, log it as `model_downgraded=true` in metadata.
- Track `consecutive_downgrades` per session. After 3 consecutive downgrades, treat the
  account as saturated: set a 2-hour cooldown and stop the current session.
- The batch loop naturally dispatches a replacement worker that claims a fresh (rested) account.

### 3. "Too many requests" modal not detected
When an account exceeds its usage cap within a time window, ChatGPT shows a modal or inline
message saying "Too many requests" / "You've reached your limit" / "Try again in X minutes".
Previously the extractor did not detect this and would silently fail or produce garbage output.

**Fix:** Added `check_rate_limit_state()` to `ChatGPTRunner` — a JavaScript probe that scans
dialogs and a limited body excerpt for rate-limit patterns. Called after each successful
capture. When detected, a 2-hour cooldown is set on the profile and the session stops.

### 4. Stale profiles (expired session cookies) running logged-out
Chrome profiles stored in Tigris preserve the ChatGPT session cookie, but cookies expire.
When a worker started with an expired cookie, ChatGPT served the page in guest/unauthenticated
mode. The extractor would proceed, using the free anonymous tier which always returns
`gpt-5-3-mini` — and these were saved as normal outputs with `logged_in: false`.

In batch `de54724b`, `data@zebora.io` had 30 of its 63 `gpt-5-5` run attempts run logged-out.

**Fix:** Pre-session login check in `run_extraction_job`. Immediately after Chrome opens:
- If `login_button_present = True` (unauthenticated) → abort the entire run.
- Set a 24-hour cooldown with `reason='login_expired'` so the account is skipped until
  re-authenticated via VNC and re-uploaded to Tigris.
- Returns `status='aborted_not_logged_in'` so the batch flow can log and move on.

### 5. Only 11 of 15 available accounts used
With 6 workers running and 15 available profiles, only the first 6 accounts (by LRU order)
were ever claimed. Workers held their profile for the entire run duration (hours), so the
remaining 9 profiles were never accessed.

**Fix (partial — in progress):** The cooldown system naturally enables rotation: when an
account hits a rate limit or downgrade threshold, it cools down, releases its lock, and the
next worker to start claims the next available (non-cooled) account from the pool.

A `max_prompts_per_profile` parameter (planned) will add explicit rotation: each worker
voluntarily releases its profile after N prompts, re-acquires a fresh one, and continues.
This ensures all accounts see regular usage and none get over-saturated.

---

## Changes Made

### `automated_extraction/chatgpt_runner.py`

```python
def check_rate_limit_state(self) -> bool:
    """
    Returns True if ChatGPT is currently rate-limiting this account.
    Scans dialogs and body text for 'too many requests', 'you've reached your limit',
    'try again in', etc.
    """
```

### `automated_extraction/extraction.py`

- **Pre-session login abort**: `session_info.get("login_button_present")` → abort + 24h cooldown
- **Claim TTL 5→20 min**: `api.try_claim_prompt(..., ttl_minutes=20)`
- **Consecutive downgrade tracking**: `consecutive_downgrades` counter per session; reset to 0
  on a successful expected-model response
- **Rate-limit/downgrade rotation**: after modal detection or 3 consecutive downgrades → call
  `set_profile_cooldown(index, worker_id, cooldown_hours=2.0, reason=trigger)` and `break` out
  of the prompt loop

### `automated_extraction/profile_manager.py`

```python
def set_profile_cooldown(
    index: int,
    worker_id: str,
    cooldown_hours: float = 2.0,
    reason: str = "rate_limit",
) -> bool:
```

Wraps the `set_chatgpt_profile_cooldown` Supabase RPC.

### `migrations/002_chatgpt_profiles_cooldown.sql`

- `ALTER TABLE chatgpt_profiles ADD COLUMN cooldown_until TIMESTAMPTZ`
- `ALTER TABLE chatgpt_profiles ADD COLUMN cooldown_reason TEXT`
- Updated `acquire_chatgpt_profile` RPC: adds `AND (cooldown_until IS NULL OR cooldown_until < NOW())`
- New RPCs: `set_chatgpt_profile_cooldown`, `clear_chatgpt_profile_cooldown`

---

## Cooldown Management

### Check current cooldowns
```sql
SELECT "index", email, cooldown_until, cooldown_reason
FROM chatgpt_profiles
WHERE cooldown_until > NOW()
ORDER BY cooldown_until;
```

### Clear a cooldown after re-login
```sql
SELECT clear_chatgpt_profile_cooldown(5);  -- by index
```

### Cooldown reasons
| Reason | Cooldown | Triggered by |
|---|---|---|
| `rate_limit` | 2 hours | "too many requests" modal detected |
| `consecutive_downgrades` | 2 hours | 3+ gpt-5-5→mini downgrades in one session |
| `login_expired` | 24 hours | Login button detected at session start |
| `cloudflare` | Set manually | Persistent IP block (machine-level, not account) |

---

## Scheduling Recommendations

Based on the batch analysis:

| Account type | gpt-5-5 capacity | Recommendation |
|---|---|---|
| Fresh/rested account | First ~30–50 prompts | Rotate every 30 prompts (planned) |
| Account with recent activity | Downgrades quickly | Allow cooldown; re-acquire after 2h |
| Logged-out account | 0 gpt-5-5 outputs | Re-login via VNC; do not use until re-uploaded |

**Optimal throughput:** ~100–140 outputs/hour with 5–6 concurrent workers on healthy, logged-in accounts.

---

## Account Pool Status (as of 2026-06-28)

| Index | Email | Status |
|---|---|---|
| 0 | dev@theround.com | Disabled |
| 1 | chris@theround.com | Disabled |
| 2 | bob@theround.com | Disabled |
| 3–17 | frank, info, dev, data, rob, john, anna, alice, cleo, ryan, steve, ian, laura, lisa, emily | Active |

15 active accounts available. All were logged in via VNC on 2026-06-28 and profiles saved to Tigris.
