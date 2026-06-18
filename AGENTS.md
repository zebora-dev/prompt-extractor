# prompt-extractor — Agent Context

Before doing anything else, read these files in order:
1. .standards/general/conventions.md
2. .standards/python/conventions.md
3. .standards/prefect/conventions.md

---

# prompt-extractor

Browser-based extraction of LLM responses (ChatGPT etc.) using Selenium + Prefect.
Feeds brand response data into brand-score-pipeline.

## Stack

- Python 3.12+, uv, Prefect 3, Selenium + undetected-chromedriver
- Supabase (storage), Fly.io (deployment)

## Local dev

```sh
uv sync
cp .env.example .env
python -m pytest
```

## Repo structure

```
automated_extraction/   Main extraction package
scripts/                Utility scripts
migrations/             DB migrations
tests/
```
