# Production-Readiness Roadmap

Ranked by impact / effort. Items marked **[quick win]** are low-effort and high-value.

---

## 1. Reliability & Resilience

### 1.1 Per-prompt retry with exponential backoff
**Why:** Transient network errors, rate-limit blips, and brief JS loading delays cause single-prompt failures that abort an otherwise-healthy batch.  
**What:** Wrap the inner extraction loop with a retry decorator (e.g. `tenacity`) that catches `WebDriverException` and Selenium timeouts, backs off, and retries up to 3×. Only move to the next prompt on permanent failure.

### 1.2 Session health-check before each prompt
**Why:** Chrome sessions can silently die mid-batch, leading to a flood of `NoSuchWindowException` errors and partial batches.  
**What:** Before issuing each prompt, verify the driver session is alive (`driver.current_url` guarded try/except). If dead, restart the runner and re-authenticate.

### 1.3 Atomic save with conflict detection
**Why:** Network interruptions mid-save can leave duplicate or partial rows in Supabase.  
**What:** Use an upsert (insert + `on_conflict=...`) keyed on `(batch_id, prompt_id, llm_model)` for all `prompts_outputs` writes.

### 1.4 Structured failure reporting
**Why:** Today a failed prompt sets `failed_count` but the caller can't identify *which* prompts failed or why.  
**What:** Append a `FailedPrompt(prompt_id, error, traceback)` list to the extraction result. Log and optionally save to a `prompts_outputs_errors` table or include in the Prefect artifact.

---

## 2. Observability

### 2.1 Prefect artifacts for every run **[quick win]**
**Why:** The Prefect UI shows flow state (pass/fail) but not what was actually captured.  
**What:** Call `create_markdown_artifact()` at the end of each task with a summary table: prompts attempted, saved, skipped, failed, PAA rows, and capture states.

### 2.2 Structured logging with run context **[quick win]**
**Why:** Log lines from concurrent workers are interleaved and hard to correlate.  
**What:** Add `batch_id`, `prompt_id`, and `provider` to every log record via a `logging.Filter` or `structlog` context vars. Works with the existing `logging.basicConfig` setup.

### 2.3 Prometheus / StatsD metrics
**Why:** Long-running extractions need real-time visibility — queue depth, success rate, clipboard-capture vs. DOM-fallback ratio.  
**What:** Emit counters via `prefect.runtime` or a lightweight `statsd` client. A Fly.io sidecar (or Grafana Cloud push) can scrape them.

### 2.4 Screenshot on failure
**Why:** Hard to diagnose why a prompt failed without knowing what the browser was showing at the moment.  
**What:** In the exception handler inside `_capture_single_paa` and the main capture loops, call `driver.save_screenshot(f"/tmp/error_{prompt_id}.png")` and upload as a Prefect artifact.

---

## 3. Correctness

### 3.1 DOM selector regression tests **[quick win]**
**Why:** Google and ChatGPT update their DOM regularly and break selectors silently. The existing test suite mocks the browser entirely.  
**What:** Add a small fixture-based test that loads a saved HTML snapshot and asserts the JS extraction scripts return the expected fields. Already have `tests/test_google_suggestions.html` as a starting point — wire it into pytest.

### 3.2 `capture_state` enum validation
**Why:** `capture_state` is a free-form string. Callers can't reliably branch on it.  
**What:** Define an `Enum` (e.g. `CaptureState`) and use it everywhere. Add a `validate_capture_state` helper that maps unknown strings to `CaptureState.UNKNOWN` rather than crashing.

### 3.3 PAA source URL round-trip test **[quick win]**
**Why:** `clean_google_url` and `unwrapGoogleUrl` (JS) must agree. They've diverged before.  
**What:** Add a parametrized pytest with a table of raw Google redirect URLs and their expected clean equivalents, tested against the Python function.

---

## 4. Security & Secrets

### 4.1 Rotate `BRANDSIGHT_SUPABASE_ANON_KEY` detection **[quick win]**
**Why:** The anon key is committed in `.env.example` comments and appears in logs.  
**What:** Add a `bandit` rule (or custom pre-commit hook) that rejects commits containing strings matching the Supabase key pattern (`eyJ...`). Already have `bandit` in CI — extend `pyproject.toml` with a `B105`/`B106` suppression audit.

### 4.2 Secrets scanning in CI **[quick win]**
**Why:** `CHATGPT_ACCOUNTS_B64` encodes credentials. A developer could accidentally log or commit a decoded version.  
**What:** Add `gitleaks` (or GitHub's push protection + secret scanning) to the repository settings and CI workflow.

### 4.3 Chrome profile volume encryption
**Why:** The Fly.io persistent volume (`/data`) stores real Chrome sessions (cookies, local storage) for logged-in ChatGPT accounts.  
**What:** Document (and optionally script) `fly volumes` encryption-at-rest for the `/data` volume. Fly volumes already support this via the `--encrypted` flag.

---

## 5. Performance

### 5.1 Parallel prompt extraction across providers
**Why:** AI Mode and AI Overview runs for the same batch are fully independent but run serially today.  
**What:** In Prefect, submit both flows as concurrent sub-flow runs using `.submit()`. Each needs its own Chrome instance (already isolated by `chrome_user_data_dir`).

### 5.2 Reduce inter-prompt sleep
**Why:** Fixed `time.sleep(0.5)` calls in PAA capture and clipboard intercepts add 2-5 s per prompt with no adaptive backoff.  
**What:** Replace hard sleeps with `WebDriverWait` + `expected_conditions` where a DOM condition is available. Fall back to a configurable `MIN_PAUSE_SECONDS` env var (default: 0.2).

### 5.3 Chrome cold-start warm-up
**Why:** Each `with GoogleAIModeRunner(...) as runner:` block re-launches Chrome, which takes ~3 s.  
**What:** Keep the driver alive across prompts within a batch (already done) — but also pre-warm the browser before the first prompt is fetched from the API to hide API latency.

---

## 6. Maintainability

### 6.1 Shared `BaseGoogleRunner` **[quick win]**
**Why:** `GoogleAIModeRunner` and `GoogleAIOverviewRunner` share ~70% of their logic (`build_search_url`, `_intercept_clipboard`, PAA wiring).  
**What:** Extract a `BaseGoogleRunner` with shared methods. The two subclasses only override `build_search_url` and the AI panel detection logic.

### 6.2 Pin third-party package versions in `requirements.txt`
**Why:** `undetected-chromedriver` and `selenium` update frequently and break automation scripts without warning.  
**What:** Pin all deps to exact versions (`==`) in `requirements.txt`. Use `pip-compile` (pip-tools) to keep the lock file reproducible. Add a monthly Dependabot or Renovate config to propose version bumps via PR.

### 6.3 Typed `ApiClient` responses
**Why:** `api_client.py` returns raw `dict` / `list` from Supabase with no type information, making callers rely on string keys.  
**What:** Define typed `TypedDict` or dataclass response wrappers for each table and annotate `ApiClient` methods. Pairs well with the existing mypy advisory checks.

---

## 7. Deployment

### 7.1 Health-check endpoint on Fly.io
**Why:** Fly.io can't distinguish a healthy idle worker from a crashed one without a health check.  
**What:** Add a minimal FastAPI (or `http.server`) endpoint at `/health` that returns `200 OK`. Configure `[http_service]` checks in `fly.toml`.

### 7.2 Chrome version pinning in `Dockerfile`
**Why:** `apt-get install chromium-browser` picks up whatever version Debian ships, which may not match the `undetected-chromedriver` version.  
**What:** Pin to a specific Chromium version in the Dockerfile and test the pair on each Dependabot bump.

### 7.3 Multi-region Fly.io machines
**Why:** Google Search and ChatGPT responses vary by geography. Separate Fly machines per region give clean geographic isolation.  
**What:** Add `[[regions]]` entries in `fly.toml` and parameterise `GOOGLE_SEARCH_COUNTRY` per machine via per-machine secrets (`fly secrets set --app ... --region ...`).

---

## Priority order (suggested)

| # | Item | Effort | Impact |
|---|------|--------|--------|
| 1 | 2.1 Prefect artifacts | S | High |
| 2 | 3.1 DOM selector regression tests | S | High |
| 3 | 3.3 PAA source URL round-trip test | S | Medium |
| 4 | 2.2 Structured logging | S | High |
| 5 | 4.2 Secrets scanning in CI | S | High |
| 6 | 6.1 Shared BaseGoogleRunner | M | Medium |
| 7 | 1.1 Per-prompt retry | M | High |
| 8 | 1.3 Atomic upsert | M | High |
| 9 | 1.4 Structured failure reporting | M | High |
| 10 | 6.2 Pin package versions | S | High |
| 11 | 7.1 Health-check endpoint | M | Medium |
| 12 | 5.1 Parallel provider runs | L | Medium |
| 13 | 1.2 Session health-check | M | Medium |
| 14 | 7.2 Chrome version pinning | M | Medium |
| 15 | 5.2 Reduce inter-prompt sleep | M | Low |
