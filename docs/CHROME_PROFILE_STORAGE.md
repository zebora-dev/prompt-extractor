# Chrome Profile Snapshot Storage

Scope of work for persisting pre-logged-in ChatGPT Chrome profiles in Supabase Storage so that cloned Fly.io workers can start sessions without auto_login or 2FA.

---

## Problem

- Fly.io volumes are **single-attach** — clones cannot share the original machine's `/data` volume.
- Clones start with an empty `/tmp/chrome-profile`, which means no cookies, no session.
- `auto_login=True` is unreliable because fresh profiles trigger 2FA on every login.
- We need a way to pre-bake logged-in Chrome profiles and distribute them to clones at startup.

---

## Solution Overview

```
┌─────────────────────────────────────────────────┐
│  Supabase Storage bucket: chrome-profiles        │
│                                                  │
│  profile_0.tar.gz  ← ChatGPT account 0          │
│  profile_1.tar.gz  ← ChatGPT account 1          │
│  profile_2.tar.gz  ← ChatGPT account 2          │
└─────────────────────────────────────────────────┘
        ↑ one-time capture (VNC)     ↓ restore on startup
┌──────────────────────┐    ┌─────────────────────────────┐
│  VNC Session         │    │  Clone / Original worker     │
│  (manual login)      │    │  CHROME_PROFILE_INDEX=2      │
│                      │    │                              │
│  python -m ...       │    │  entrypoint.sh downloads     │
│  --capture-profile 0 │    │  profile_2.tar.gz            │
│  → logs in manually  │    │  → extracts to               │
│  → uploads snapshot  │    │    $CHATGPT_CHROME_USER_DATA_DIR
└──────────────────────┘    └─────────────────────────────┘
```

---

## Current Status

### ✅ DONE — Prototype (Phase 1)

| Component | File | Status |
|---|---|---|
| `ProfileManager` — upload/download/exists | `automated_extraction/profile_manager.py` | ✅ Built |
| CLI `--capture-profile INDEX` command | `automated_extraction/cli.py` | ✅ Built |
| Entrypoint profile restore on startup | `docker/entrypoint.sh` | ✅ Built |

**What the prototype covers:**
- Manual capture workflow via VNC: open Chrome → log in → press Enter → snapshot uploaded
- Automatic restore on worker startup when `CHROME_PROFILE_INDEX` env var is set
- Standalone CLI for upload/restore/exists checks
- Uses existing Supabase credentials — no new infrastructure required
- Uses `BRANDSIGHT_SUPABASE_SERVICE_KEY` to bypass RLS and 50 MB file limit
- Cache dirs (`Cache`, `Code Cache`, `GPUCache`, etc.) excluded from archive to keep size small
- Tested end-to-end: capture → upload → restore → Chrome opens pre-logged-in ✅

### ✅ DONE — Multi-worker profile assignment (Phase 2)

| Component | File | Status |
|---|---|---|
| `clone_machine()` accepts `profile_index` | `automated_extraction/fly_scaler.py` | ✅ Built |
| `scale_up()` injects index per clone | `automated_extraction/fly_scaler.py` | ✅ Built |

**How it works:**
- Set `CHROME_PROFILE_TOTAL_ACCOUNTS=N` as a Fly secret (where N = number of uploaded profiles)
- When `scale_up()` creates clones, each gets `CHROME_PROFILE_INDEX` injected: clone 0 → index 0, clone 1 → index 1, etc.
- Wraps round-robin if there are more clones than accounts (e.g. 3 accounts, 6 clones → 0,1,2,0,1,2)
- If `CHROME_PROFILE_TOTAL_ACCOUNTS` is unset or 0, no index is injected (safe default — falls back to empty profile)

---

## How to Use (Prototype)

### Step 1 — Create the Supabase Storage bucket

In the Supabase dashboard → Storage → New bucket:
- Name: `chrome-profiles`
- Public: **No** (private)

### Step 2 — Capture a profile (run on worker via VNC or locally)

```bash
# Capture account 0 — opens Chrome, wait for manual login, then press Enter
python -m automated_extraction \
  --capture-profile 0 \
  --chrome-user-data-dir /tmp/chrome-capture-0

# Repeat for each account
python -m automated_extraction --capture-profile 1 --chrome-user-data-dir /tmp/chrome-capture-1
python -m automated_extraction --capture-profile 2 --chrome-user-data-dir /tmp/chrome-capture-2
```

Or use the profile manager directly:
```bash
python -m automated_extraction.profile_manager upload --index 0 --dir /tmp/chrome-capture-0
python -m automated_extraction.profile_manager exists --index 0
```

### Step 3 — Test restore locally

```bash
python -m automated_extraction.profile_manager restore --index 0 --dest /tmp/test-restore
```

### Step 4 — Deploy and test on a worker

Set the env var on the machine (in `fly-uk.yaml` for originals, or injected by scaler for clones):

```yaml
env:
  CHROME_PROFILE_INDEX: "0"
```

On next startup, the entrypoint will automatically download and restore the profile before the Prefect worker starts.

### Step 5 — Run an extraction

```bash
python -m automated_extraction \
  --batch-id <uuid> \
  --limit 1 \
  --no-headless  # to verify session is active
```

Expected: Chrome opens already logged in to ChatGPT — no login page, no 2FA.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CHROME_PROFILE_INDEX` | *(unset)* | Profile slot to restore on startup. Set on each machine. |
| `CHROME_PROFILE_BUCKET` | `chrome-profiles` | Supabase Storage bucket name |
| `BRANDSIGHT_SUPABASE_URL` | *(required)* | Already set — reused from existing config |
| `BRANDSIGHT_SUPABASE_ANON_KEY` | *(required)* | Already set — reused from existing config |

---

## Remaining Work (Phases 2 & 3)

### Phase 2 — Multi-worker profile assignment

**Goal:** Each clone automatically gets the right profile index without manual config.

**Changes needed:**

#### `fly_scaler.py` — inject `CHROME_PROFILE_INDEX` per clone

```python
# In clone_machine(), after setting FLY_CLONE_LABEL:
total_accounts = int(os.getenv("CHROME_PROFILE_TOTAL_ACCOUNTS", "1"))
clone_index = i  # loop index from scale_up()
env["CHROME_PROFILE_INDEX"] = str(clone_index % total_accounts)
```

The `i` variable is already available in the `scale_up()` clone loop:
```python
for i in range(needed):
    label = f"{ts}-{i}"
    new_machine = client.clone_machine(app_name, source_id, label)
```

`clone_machine()` needs to accept `profile_index` as an optional parameter, or the env injection can happen in the caller before passing to `clone_machine()`.

#### New env var

```bash
CHROME_PROFILE_TOTAL_ACCOUNTS=3  # how many profiles exist; clones wrap round-robin
```

#### `fly-uk.yaml` — static index for originals

Each original machine gets a fixed index:
```yaml
# Machine 1 (original-1): CHROME_PROFILE_INDEX=0
# Machine 2 (original-2): CHROME_PROFILE_INDEX=1
# etc.
```

---

### Phase 3 — Profile refresh after run

**Goal:** Re-upload the profile after each successful extraction to keep cookies fresh.

**Changes needed:**

#### `extraction.py` — optional upload at end of job

```python
# At the end of run_extraction_job(), if profile_index is set:
if profile_index is not None and not dry_run:
    from .profile_manager import upload_profile
    upload_profile(profile_index, chrome_user_data_dir)
```

#### New flow parameter

Add `upload_profile_after_run: bool = False` to `prompt_extraction_flow` and `prompt-extraction-batch`.

#### When to refresh

- After every run: keeps cookies maximally fresh but adds ~10s upload time per batch
- Daily/scheduled: a separate Prefect flow that restores, opens Chrome (headless), and re-uploads
- On cookie expiry detection: if extraction fails with a login redirect, trigger re-capture

---

### Phase 4 — Dispatcher profile assignment

**Goal:** Dispatcher passes the correct `login_email` (matching the profile) to each worker, so logs and claims show the right account.

**Changes needed in `dispatcher.py`:**

```python
accounts_raw = os.environ.get("CHATGPT_ACCOUNTS_B64", "")
available_accounts = list(json.loads(base64.b64decode(accounts_raw)).keys()) if accounts_raw else []

for worker_index in range(effective_workers):
    worker_params = base_params.copy()
    if available_accounts:
        worker_params["login_email"] = available_accounts[worker_index % len(available_accounts)]
    # submit flow with worker_params
```

---

## Architecture Diagram (Full System)

```
                    ┌──────────────────────────────────────┐
                    │   Supabase Storage: chrome-profiles   │
                    │   profile_0.tar.gz (account 0)        │
                    │   profile_1.tar.gz (account 1)        │
                    │   profile_2.tar.gz (account 2)        │
                    └──────────────────────────────────────┘
                         ↑                    ↓
                  [Phase 3: refresh]   [entrypoint restore]
                         │                    │
          ┌──────────────┴──┐      ┌──────────┴───────────────────────┐
          │  Original-0     │      │  Clone-0    Clone-1    Clone-2   │
          │  INDEX=0        │      │  INDEX=0    INDEX=1    INDEX=2   │
          │  /data/chrome-  │      │  /tmp/chrome-profile (ephemeral) │
          │  profile        │      │                                  │
          └─────────────────┘      └──────────────────────────────────┘
                    ↑                         ↑
             [fly-uk.yaml]            [fly_scaler.py Phase 2]
             static assignment        dynamic injection

Dispatcher (Phase 4):
  worker 0 → login_email=account_0@...  → Clone-0 (INDEX=0) → profile_0
  worker 1 → login_email=account_1@...  → Clone-1 (INDEX=1) → profile_1
  worker 2 → login_email=account_2@...  → Clone-2 (INDEX=2) → profile_2
```

---

## Testing Checklist

- [ ] Supabase `chrome-profiles` bucket created
- [ ] Profile 0 captured and uploaded via `--capture-profile 0`
- [ ] Profile 0 restore verified locally (`profile_manager restore`)
- [ ] Worker deployed with `CHROME_PROFILE_INDEX=0` — starts pre-logged-in
- [ ] Single extraction run confirms no 2FA / login redirect
- [ ] (Phase 2) Clone spawned with injected index — confirms correct profile loaded
- [ ] (Phase 3) Profile re-uploaded after run — re-deploy confirms refreshed cookies

---

## Notes

- Profile size is typically **50–200 MB** per account (Chrome caches can be trimmed with `--disk-cache-size=1` if needed)
- Supabase Storage has a **50 MB file size limit on the free tier** — upgrade to Pro ($25/mo) or use the service key to bypass the limit for larger profiles
- The restore in `entrypoint.sh` fails open — if the download fails, the worker starts with an empty profile rather than crashing
- `BRANDSIGHT_SUPABASE_ANON_KEY` may be subject to RLS on the storage bucket; consider switching the profile manager to use `BRANDSIGHT_SUPABASE_SERVICE_KEY` for reliable access (aligns with ROADMAP item 4.3)
