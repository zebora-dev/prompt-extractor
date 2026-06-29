# gpt-extractor-uk — ChatGPT Extraction Workers (UK)

Stateless Fly.io workers for ChatGPT prompt extraction, UK region (London/lhr).

Unlike the legacy `prompt-extractor-uk` app, workers here have **no Fly volumes**.
Chrome profiles are stored as compressed archives in Tigris object storage and
dynamically claimed at startup — any worker can run any account.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Fly.io: gpt-extractor-uk (lhr)                             │
│                                                             │
│  Worker A  ──claim──▶  chatgpt_profiles (Supabase)         │
│  Worker B  ──claim──▶  (FOR UPDATE SKIP LOCKED)            │
│                                │                            │
│            ──download──▶  Tigris: gpt-extractor-profiles   │
│                                │                            │
│            /tmp/chrome-profile ◀── extracted session        │
│            (ephemeral — no volume)                          │
│                                                             │
│  On stop:  profile re-archived ──upload──▶  Tigris          │
│            claim released ──────────────▶  Supabase         │
└─────────────────────────────────────────────────────────────┘
```

### Key properties

- **Stateless machines** — no Fly volumes, no per-machine account assignment
- **Atomic claiming** — Postgres `SELECT FOR UPDATE SKIP LOCKED` prevents two workers ever using the same account simultaneously
- **Automatic session persistence** — on any exit (SIGTERM, error, normal), an `EXIT` trap re-archives the Chrome profile and uploads it to Tigris before releasing the claim
- **Failure recovery** — claims have a 4-hour TTL (`lock_expires_at`). If a machine is hard-killed (SIGKILL, power failure), the account auto-releases after 4 hours so other workers can pick it up
- **Scale freely** — spin up as many machines as you have available accounts; add accounts without touching machines

---

## VM spec

`performance-2x` — 2 CPUs, 4096 MB RAM (required for Chrome + headless extraction).

---

## Adding a new account

1. **Add a row to `chatgpt_profiles`** in the Supabase SQL editor:

   ```sql
   INSERT INTO chatgpt_profiles ("index", email)
   VALUES (12, 'newaccount@example.com');
   ```

   Use the next available index. Check existing rows with:
   ```sql
   SELECT "index", email, is_locked FROM chatgpt_profiles ORDER BY "index";
   ```

2. **Start a worker** (any stopped machine works):
   ```bash
   flyctl machines start <machine-id> -a gpt-extractor-uk
   ```
   The worker will claim the new account (it has no `last_uploaded_at` so it's prioritised).

3. **Log in via VNC** — the machine URL is printed in the Slack Cloudflare alert, or construct it:
   ```
   https://gpt-extractor-uk.fly.dev/vnc/<machine-id>/vnc.html?autoconnect=true&path=vnc/<machine-id>/websockify
   ```
   Navigate to chatgpt.com and complete login.

4. **Stop the machine**:
   ```bash
   flyctl machines stop <machine-id> -a gpt-extractor-uk
   ```
   Watch the logs — you should see the profile archive uploaded to Tigris and the claim released. The account is now ready for any worker to pick up without VNC.

---

## Starting / stopping workers

```bash
# List machines and their state
flyctl machines list -a gpt-extractor-uk

# Start specific machines
flyctl machines start <id1> <id2> -a gpt-extractor-uk

# Stop specific machines (triggers profile upload + claim release)
flyctl machines stop <id1> <id2> -a gpt-extractor-uk
```

Workers connect to the `gpt-extraction-uk` Prefect work pool and process one flow at a time (`--limit 1`).

---

## Deploying a new image

`fly deploy` fails on this app because there are no volumes to bind to. Always update machines individually:

```bash
# Build and get the new image tag
flyctl deploy -a gpt-extractor-uk -c fly-gpt-uk.yaml 2>&1 | grep "^image:"

# Update each machine
IMAGE="registry.fly.io/gpt-extractor-uk:deployment-<tag>"
for ID in $(flyctl machines list -a gpt-extractor-uk --json | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)]"); do
  flyctl machine update $ID -a gpt-extractor-uk --image $IMAGE --yes
done
```

---

## Database: `chatgpt_profiles` table

| Column | Description |
|---|---|
| `index` | Unique integer slot number |
| `email` | ChatGPT account email |
| `is_locked` | `true` = not available to the pool |
| `locked_by` | `FLY_MACHINE_ID` of the current holder, or `'disabled'` for permanently locked accounts |
| `lock_expires_at` | Safety TTL — claim auto-expires after 4 hours |
| `last_uploaded_at` | Stamped on every **acquire** (not just upload) — used as the LRU sort key to distribute load |
| `cooldown_until` | When set, the account is skipped by `acquire_chatgpt_profile` until this timestamp passes |
| `cooldown_reason` | Why the cooldown was set: `rate_limit`, `login_expired`, `cloudflare`, `consecutive_downgrades`, or `gpt55_session_cap` |

### Profile selection ordering

`acquire_chatgpt_profile` uses a two-tier ordering to distribute load evenly:

1. **30-minute rest threshold** — accounts used in the last 30 minutes are deprioritised. Accounts idle for longer are always preferred, preventing the same accounts being reclaimed immediately after a cooldown expires.
2. **Longest-rested first** — within each tier, the account with the oldest `last_uploaded_at` is picked. New accounts with no upload history are always initialised first.

**Critical:** `last_uploaded_at` is stamped at **acquire time** (not just on Tigris upload). This means that even if a session is cancelled or crashes before the profile is uploaded, the account still moves to the back of the queue. Without this, cancelled sessions left `last_uploaded_at` frozen at the previous clean upload — causing burned-out accounts to look like the most rested and be selected repeatedly.

To manually backfill an account's position (e.g. after force-cancelling sessions):
```sql
UPDATE chatgpt_profiles SET last_uploaded_at = NOW() WHERE "index" IN (3, 4, 10);
```

### `chatgpt_profile_stats` view

A read-only view for monitoring per-account output history:

```sql
SELECT * FROM chatgpt_profile_stats ORDER BY "index";
```

| Column | Description |
|---|---|
| `status` | `available`, `in_use`, `cooled`, or `disabled` |
| `last24h_gpt55` | gpt-5-5 outputs in the last 24 hours |
| `last24h_mini` | gpt-5-3-mini outputs in the last 24 hours |
| `last24h_gpt55_pct` | Percentage of last-24h outputs that were gpt-5-5 |
| `last_run_at` | Timestamp of the most recent output from this account |
| `lifetime_gpt55` / `lifetime_mini` | All-time output counts |

To disable an account without deleting it:
```sql
UPDATE chatgpt_profiles
SET is_locked = true, locked_by = 'disabled', lock_expires_at = '2099-01-01'
WHERE "index" = 12;
```

To re-enable:
```sql
UPDATE chatgpt_profiles
SET is_locked = false, locked_by = NULL, lock_expires_at = NULL
WHERE "index" = 12;
```

To manually clear a cooldown (e.g. after re-logging in via VNC):
```sql
SELECT clear_chatgpt_profile_cooldown(12);
-- or directly:
UPDATE chatgpt_profiles SET cooldown_until = NULL, cooldown_reason = NULL WHERE "index" = 12;
```

To see which accounts are currently cooling down:
```sql
SELECT "index", email, cooldown_until, cooldown_reason
FROM chatgpt_profiles
WHERE cooldown_until > NOW()
ORDER BY cooldown_until;
```

---

## gpt-5-5 capture behaviour

Free ChatGPT accounts serve `gpt-5-5` for approximately 30–50 prompts after a rest period, then downgrade to `gpt-5-3-mini`. The worker tracks this per session and rotates proactively:

| Trigger | Condition | Cooldown | Behaviour |
|---|---|---|---|
| `gpt55_session_cap` | ≥40 gpt-5-5 outputs captured this session | 30 min | Account has delivered its budget — release early so it can rest |
| `consecutive_downgrades` | 1st mini output once ≥20 gpt-5-5 captured; or 3rd mini if fewer than 20 | 30 min | Account has downgraded — rotate to a fresher account |
| `rate_limit_modal` | ChatGPT "Too many requests" modal detected | 2 hours | Hard rate limit — needs a longer rest |
| `login_expired` | Session cookie invalid at startup | 24 hours | Requires manual VNC re-login |

The 30-minute cooldown for downgrade/cap triggers (down from 2h) reflects that accounts can recover their gpt-5-5 window faster than previously assumed. After 30 minutes, the account re-enters the pool and will be picked again once it has been idle longer than other available accounts.

Both `gpt-5-5` and `gpt-5-3-mini` outputs are always saved to `prompts_outputs` — workers never discard a response. The rotation only stops further prompts being sent to a downgraded account so a fresher one can take over for gpt-5-5 captures.

---

## Tigris storage

Profiles are stored at `chatgpt/profile_<index>.tar.gz` in the `gpt-extractor-profiles` Tigris bucket.

Cache directories (`Cache/`, `Code Cache/`, `GPUCache/`, etc.) are excluded from the archive to keep files small (~40–60 MB per account). Only cookies, login state, and preferences are preserved — everything needed to stay logged into ChatGPT.

Tigris credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `BUCKET_NAME`, `AWS_ENDPOINT_URL_S3`) are injected automatically by Fly as secrets.

---

## Secrets

Set once on the app — never stored in config files:

```bash
flyctl secrets set -a gpt-extractor-uk \
  BRANDSIGHT_API_BASE_URL="..." \
  BRANDSIGHT_SUPABASE_URL="..." \
  BRANDSIGHT_SUPABASE_ANON_KEY="..." \
  BRANDSIGHT_SUPABASE_SERVICE_KEY="..." \
  PREFECT_API_URL="https://prompt-extractor-prefect.fly.dev/api" \
  SLACK_BOT_TOKEN="..." \
  FLY_API_TOKEN="..." \
  GOOGLE_PROXY_URL="..."
```

Tigris credentials are added automatically when the storage bucket is created with `flyctl storage create`.

---

## Troubleshooting

**Worker claims an account but Chrome starts at the login page**
The profile in Tigris may be stale or the session cookie expired. VNC in, log in again, and stop the machine to re-upload the fresh session.

**Worker keeps claiming the same account on restart**
Expected — `acquire_chatgpt_profile` allows a worker to re-acquire its own slot on restart (covers crash/restart cycles without waiting for the TTL).

**All accounts show as locked / no available accounts**
Check the pool:
```sql
SELECT "index", email, is_locked, locked_by, lock_expires_at
FROM chatgpt_profiles
ORDER BY "index";
```
If claims are stuck (machine died mid-job), they'll auto-expire after 4 hours. To force-release immediately:
```sql
UPDATE chatgpt_profiles
SET is_locked = false, locked_by = NULL, lock_expires_at = NULL
WHERE locked_by != 'disabled' AND lock_expires_at < NOW();
```

**Profile upload failed on stop**
The profile upload is best-effort — a failure logs a warning but doesn't block shutdown. The claim TTL will expire after 4 hours. The account will then be claimable again, but the next worker will start with whatever was last successfully uploaded (or a fresh profile if nothing was ever saved).
