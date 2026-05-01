# Automated ChatGPT Extraction

Standalone automation for pulling prompts from the BrandSight API, running them in ChatGPT, capturing the answer via the ChatGPT copy button, and saving outputs back to the existing `prompt-outputs` API.

This mirrors the Chrome/Firefox extension flow in `chromeApp/extension-shared/background.js`, but runs as a CLI process.

## Setup

```bash
cd automated-extraction
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Python 3.12 is recommended because `undetected-chromedriver` currently imports `distutils`, which Python 3.13 removed. If you run on Python 3.13, the automation falls back to Selenium's standard Chrome driver.

Edit `.env` and set `BRANDSIGHT_SUPABASE_ANON_KEY`. The current extension value is in:

```text
chromeApp/extension-shared/background.js
```

## Login Session

By default, the automation stores a dedicated logged-in browser profile at:

```text
automated-extraction/.chrome-profile
```

Create or refresh that ChatGPT login session with:

```bash
python -m automated_extraction --login-only
```

The browser will open ChatGPT. Log in manually if prompted. Once the ChatGPT prompt box is visible, the CLI exits and later runs reuse that session.

You can also choose your own dedicated Chrome profile by setting:

```text
CHATGPT_CHROME_USER_DATA_DIR=/absolute/path/to/profile
```

## Run a Batch

```bash
python -m automated_extraction --batch-id <batch-uuid>
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
