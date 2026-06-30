# Automated Extraction

Standalone automation for pulling prompts from the BrandSight API, running
them through ChatGPT or Google AI (Mode / Overview), capturing the responses,
and saving outputs back to Supabase. Scoring is triggered automatically after
each save.

This mirrors the Chrome/Firefox extension flow in
`chromeApp/extension-shared/background.js`, but runs as a headless CLI process
on a fleet of dedicated Fly.io workers.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/GPT_WORKERS.md](docs/GPT_WORKERS.md) | **ChatGPT worker fleet** — Fly.io machines, persistent Chrome, VNC login, Cloudflare handling, Slack alerts, deploy process |
| [docs/GOOGLE_AI_OVERVIEW_IMPROVEMENTS.md](docs/GOOGLE_AI_OVERVIEW_IMPROVEMENTS.md) | **Google AI Overview reliability** — CAPTCHA wait + VNC notify, no-output watchdog, auto-cooldown, stall detection, timing jitter |
| [docs/DISPATCH_MONITOR_IMPROVEMENTS.md](docs/DISPATCH_MONITOR_IMPROVEMENTS.md) | **Dispatch monitor improvements** — stale lock release, flow reconciliation, zero-output rotation, quality-aware claiming |
| [docs/PREFECT.md](docs/PREFECT.md) | **Prefect operations** — flows, parameters, triggering batches, troubleshooting |
| [slack-app-manifest.json](slack-app-manifest.json) | **Slack app manifest** — upload to api.slack.com to create the BrandSight Extractor bot |

---

## Setup

```bash
cd automated-extraction
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `BRANDSIGHT_SUPABASE_ANON_KEY`. The current extension value is in:

```text
chromeApp/extension-shared/background.js
```

For reliable login, create or choose a dedicated Chrome profile and set:

```text
CHATGPT_CHROME_USER_DATA_DIR=/absolute/path/to/profile
```

The first run opens ChatGPT and waits while you log in manually. Later runs reuse that profile.

## Automated Login (opt-in)

For environments where manual login is not practical (CI, headless workers, scheduled Prefect runs), the runner can drive the ChatGPT login flow itself. The implementation is ported from [`daily-coding-problem/chatgpt-scraper-lib`](https://github.com/daily-coding-problem/chatgpt-scraper-lib) and supports both **Basic** (OpenAI email + password) and **Google** SSO, with TOTP-based 2FA via `pyotp`.

The flow is gated by an explicit variable so existing manual-profile setups are not affected.

### Environment variables

```text
CHATGPT_AUTO_LOGIN=true
CHATGPT_LOGIN_EMAIL=automation@example.com
CHATGPT_ACCOUNTS_B64=<base64 of accounts.json>
```

### `accounts.json` shape

```json
{
  "automation@example.com": {
    "provider": "basic",
    "password": "secret-password",
    "secret": {
      "chatgpt": "JBSWY3DPEHPK3PXP"
    }
  },
  "automation-google@example.com": {
    "provider": "google",
    "password": "google-account-password",
    "secret": {
      "google":  "BASE32_GOOGLE_TOTP",
      "chatgpt": "BASE32_CHATGPT_SECONDARY_TOTP"
    }
  }
}
```

`provider` selects `BasicLogin` or `GoogleLogin`. `secret.chatgpt` is the TOTP secret OpenAI shows when you enable Authenticator-app 2FA. `secret.google` is the Google Authenticator secret (only needed for the Google provider). The Google flow also accepts an optional `secret.chatgpt` to handle the secondary "Verify Your Identity" prompt that ChatGPT may display after Google SSO.

Encode the file with:

```bash
python -c "import base64,json,sys; print(base64.b64encode(json.dumps(json.load(sys.stdin)).encode()).decode())" < accounts.json
```

### CLI overrides

```bash
python -m automated_extraction --batch-id <uuid> --auto-login --login-email automation@example.com
python -m automated_extraction --login-only --auto-login --login-email automation@example.com
```

`--auto-login`/`--no-auto-login` overrides `CHATGPT_AUTO_LOGIN`. `--login-email` overrides `CHATGPT_LOGIN_EMAIL`. The `--login-only` form runs the automated flow once and exits, useful for warming the persistent Chrome profile in CI.

### Behaviour

- `CHATGPT_AUTO_LOGIN=false` (default) — unchanged: opens ChatGPT and waits for manual login (relying on the persistent Chrome profile cookies).
- `CHATGPT_AUTO_LOGIN=true` + valid credentials — runs `BasicLogin` / `GoogleLogin` once the page loads, then verifies the prompt textarea is present. Cookies still persist into the same Chrome profile, so subsequent runs can drop the flag if you want to fall back to cookie-only mode.
- Misconfiguration (auto-login on but no email or no accounts) fails fast in `Settings.from_env(...)`.

### Maintenance note

Auth0's `auth.openai.com` and `accounts.google.com` change their DOM periodically. All login-screen selectors are kept as named constants at the top of:

- `automated_extraction/chatgpt_auth/basic_login.py`
- `automated_extraction/chatgpt_auth/google_login.py`

If automated login starts failing with a clear "selector not found" / `RuntimeError("Automated ChatGPT login failed: ...")`, those are the first files to update. TOTP secrets and decoded credentials are never logged.

## Step-by-Step Setup (with Auth Flow)

Use this if you want a repeatable setup from zero to a successful authenticated extraction run.

### 1) Install and bootstrap

```bash
cd automated-extraction
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2) Configure BrandSight API access

Edit `.env` and set:

```text
BRANDSIGHT_SUPABASE_ANON_KEY=<key-from-chromeApp/extension-shared/background.js>
```

Without this key, extraction cannot load prompts or save outputs.

### 3) Choose login mode

You have two supported modes:

- **Manual profile mode** (default): set `CHATGPT_CHROME_USER_DATA_DIR`, run once, log in manually, and reuse cookies.
- **Automated auth mode**: set `CHATGPT_AUTO_LOGIN=true` and provide account credentials (`CHATGPT_LOGIN_EMAIL` + `CHATGPT_ACCOUNTS_B64`).

### 4) Build `accounts.json` for automated auth

Create a local file (do not commit it) with one or more accounts:

```json
{
  "automation@example.com": {
    "provider": "basic",
    "password": "openai-account-password",
    "secret": {
      "chatgpt": "BASE32_CHATGPT_TOTP_SECRET"
    }
  },
  "automation-google@example.com": {
    "provider": "google",
    "password": "google-account-password",
    "secret": {
      "google": "BASE32_GOOGLE_TOTP_SECRET",
      "chatgpt": "BASE32_CHATGPT_SECONDARY_TOTP_SECRET"
    }
  }
}
```

Then base64-encode it:

```bash
python -c "import base64,json,sys; print(base64.b64encode(json.dumps(json.load(sys.stdin)).encode()).decode())" < accounts.json
```

Copy that output into:

```text
CHATGPT_ACCOUNTS_B64=<paste-output-here>
CHATGPT_AUTO_LOGIN=true
CHATGPT_LOGIN_EMAIL=automation@example.com
```

### 5) Warm the login session

Run a login-only pass first:

```bash
python -m automated_extraction --login-only --auto-login --login-email automation@example.com --verbose
```

What should happen:

1. Browser opens `chatgpt.com`.
2. Runner clicks Login.
3. It executes either `BasicLogin` or `GoogleLogin` from `chatgpt_auth/`.
4. If configured, TOTP is generated and entered.
5. Runner waits for ChatGPT prompt textarea to appear.
6. Process exits successfully.

If this command succeeds, auth flow is configured correctly.

### 6) Run a real extraction batch

```bash
python -m automated_extraction --batch-id <batch-uuid> --limit 1 --auto-login --login-email automation@example.com --verbose
```

This validates the full path: auth -> prompt send -> response copy -> `prompt-outputs` save.

### 7) Troubleshooting auth failures

- **`CHATGPT_LOGIN_EMAIL ... not present in CHATGPT_ACCOUNTS_B64`**: email key mismatch between `.env` and `accounts.json`.
- **`Automated ChatGPT login failed`** right after page load: likely selector drift on Auth0/Google; update selectors in `automated_extraction/chatgpt_auth/basic_login.py` or `automated_extraction/chatgpt_auth/google_login.py`.
- **2FA loop / code rejected**: verify TOTP secrets are base32 and correspond to the right provider (`google` vs `chatgpt`).
- **No prompt textarea after login**: session may require extra human verification; run `--login-only` non-headless and complete any challenge.

## Run a Batch

```bash
python -m automated_extraction --batch-id b4cfbc28-a046-497f-8944-65fcf10d59fe --limit 1

```

Useful options:

```bash
python -m automated_extraction --batch-id <batch-uuid> --limit 25 --skip 10
python -m automated_extraction --prompts-file ../chromeApp/extension-shared/prompts.json
python -m automated_extraction --batch-id <batch-uuid> --dry-run
```

## What It Does

1. Loads the batch and brand from `GET /batches`.
2. Loads prompts from `POST /prompts`.
3. Skips prompts already saved for the same prompt, brand, and batch.
4. Opens `https://chatgpt.com`.
5. Creates a fresh chat for each prompt where possible.
6. Sends the prompt and waits for the response to finish.
7. Clicks the latest assistant response copy button.
8. Saves the copied markdown response to `POST /prompt-outputs`.

## Notes

- The baseline library referenced by the team, `daily-coding-problem/chatgpt-scraper-lib`, is Selenium-based and uses the same core pattern: browser session, prompt textbox, send button, wait for stop button to disappear, then prefer the copy button over DOM text.
- This local implementation keeps those ideas but uses the BrandSight API payloads directly, so it can run without the extension.
- ChatGPT UI selectors can change. If capture breaks, update `automated_extraction/chatgpt_runner.py`.

