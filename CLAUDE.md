# prompt-extractor

Browser-based extraction of prompts/responses from LLM interfaces (ChatGPT etc.)
using Selenium + Prefect orchestration. Feeds brand response data into brand-score-pipeline.

## Standards

@.standards/general/conventions.md
@.standards/python/conventions.md
@.standards/prefect/conventions.md

## Stack

- Python 3.12+, uv, pyproject.toml, ruff + mypy + bandit
- Prefect 3 orchestration, Selenium + undetected-chromedriver
- Supabase (result storage), httpx
- Deployed on Fly.io (`fly.yaml`, `fly-uk.yaml`, `fly.us.toml`)

## Local dev

```sh
uv sync
cp .env.example .env          # fill in required vars
python -m pytest
```

## Key env vars

See `.env.example`. Required vars include:
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`
- `PREFECT_API_URL` / `PREFECT_API_KEY`
- Browser/login credentials for LLM platforms (never commit these)

## Repo structure

```
automated_extraction/   Main package — Selenium extraction logic
flows/                  Prefect @flow definitions (if present)
scripts/                One-off utility scripts
migrations/             DB migrations
tests/
```
