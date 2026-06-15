.PHONY: help install install-dev login run-batch run-google-ai-mode run-google-aio \
        run-google-ai-overview dry-run \
        lint format format-fix typecheck test test-cov security \
        ci validate clean \
        check-prefect prefect-server prefect-serve \
        prefect-pool prefect-pool-uk \
        prefect-deploy prefect-deploy-us prefect-deploy-uk \
        prefect-worker prefect-worker-uk prefect-list \
        deploy-worker-us deploy-worker-uk

PREFECT_WORK_POOL ?= prompt-extraction-pool

ifeq ($(origin PYTHON), undefined)
ifneq ("$(wildcard .venv/bin/python)","")
PYTHON := .venv/bin/python
else
PYTHON := python
endif
endif

help:
	@echo "Prompt Extractor"
	@echo ""
	@echo "Setup:"
	@echo "  install         Install runtime dependencies"
	@echo "  install-dev     Install runtime + dev/test dependencies"
	@echo "  login           Open ChatGPT login session using the persisted Chrome profile"
	@echo ""
	@echo "Extraction:"
	@echo "  run-batch              Run a ChatGPT batch with BATCH_ID=<uuid>"
	@echo "  run-google-ai-mode     Run a batch through Google AI Mode with BATCH_ID=<uuid>"
	@echo "  run-google-ai-overview Run a batch through Google AI Overview with BATCH_ID=<uuid>"
	@echo "  dry-run                Load prompts without opening a browser"
	@echo ""
	@echo "Quality (run locally — mirrors CI):"
	@echo "  lint            Ruff lint check"
	@echo "  format          Ruff format check"
	@echo "  typecheck       Mypy type check"
	@echo "  test            Run tests"
	@echo "  test-cov        Run tests with HTML coverage report"
	@echo "  security        Bandit security scan"
	@echo "  ci              Run all quality checks (lint + format + typecheck + test + security)"
	@echo ""
	@echo "Prefect:"
	@echo "  prefect-server      Start local Prefect server"
	@echo "  prefect-serve       Serve the prompt extraction deployment locally"
	@echo "  prefect-pool        Create the US process work pool (prompt-extraction-us)"
	@echo "  prefect-pool-uk     Create the UK process work pool (prompt-extraction-uk)"
	@echo "  prefect-deploy      Alias for prefect-deploy-us"
	@echo "  prefect-deploy-us   Register US deployments (prompt-extraction-us pool)"
	@echo "  prefect-deploy-uk   Register UK deployments (prompt-extraction-uk pool)"
	@echo "  prefect-worker      Start a process worker (PREFECT_WORK_POOL=...)"
	@echo "  prefect-worker-uk   Start a UK process worker (prompt-extraction-uk)"
	@echo "  prefect-list        List workflow deployments"
	@echo ""
	@echo "Fly.io workers:"
	@echo "  deploy-worker-us    fly deploy to prompt-extractor-us (iad)"
	@echo "  deploy-worker-uk    fly deploy to prompt-extractor-uk (lhr)"
	@echo ""
	@echo "Misc:"
	@echo "  validate        Compile Python modules"
	@echo "  clean           Remove caches and coverage artifacts"

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e . --no-deps

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e . --no-deps

# ── Quality checks ────────────────────────────────────────────────────────────

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format --check .

format-fix:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy automated_extraction --ignore-missing-imports || true

test:
	$(PYTHON) -m pytest

test-cov:
	$(PYTHON) -m pytest --cov=automated_extraction --cov-report=term-missing --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

security:
	$(PYTHON) -m bandit -r automated_extraction -c pyproject.toml \
		--severity-level medium --confidence-level medium

ci: lint format typecheck test security
	@echo "All CI checks passed ✓"

login:
	$(PYTHON) -m automated_extraction --login-only

run-batch:
	@test -n "$(BATCH_ID)" || (echo "Set BATCH_ID=<uuid>" && exit 1)
	$(PYTHON) -m automated_extraction --batch-id "$(BATCH_ID)"

run-google-ai-mode:
	@test -n "$(BATCH_ID)" || (echo "Set BATCH_ID=<uuid>" && exit 1)
	$(PYTHON) -m automated_extraction --provider google-ai-mode --batch-id "$(BATCH_ID)"

run-google-ai-overview:
	@test -n "$(BATCH_ID)" || (echo "Set BATCH_ID=<uuid>" && exit 1)
	$(PYTHON) -m automated_extraction --provider google-ai-overview --batch-id "$(BATCH_ID)"

run-google-aio: run-google-ai-overview

dry-run:
	@test -n "$(BATCH_ID)" || (echo "Set BATCH_ID=<uuid>" && exit 1)
	$(PYTHON) -m automated_extraction --batch-id "$(BATCH_ID)" --dry-run

check-prefect:
	@$(PYTHON) -c "import prefect" 2>/dev/null || (echo "Prefect is not installed for $(PYTHON). Run: make install" && exit 1)

prefect-server: check-prefect
	$(PYTHON) -m prefect server start --host 0.0.0.0

prefect-serve: check-prefect
	$(PYTHON) -m automated_extraction.workflows.register_deployments --serve

prefect-pool: check-prefect
	PREFECT_WORK_POOL="prompt-extraction-us" $(PYTHON) -m automated_extraction.workflows.register_deployments --create-pool

prefect-pool-uk: check-prefect
	PREFECT_WORK_POOL="prompt-extraction-uk" $(PYTHON) -m automated_extraction.workflows.register_deployments --create-pool

prefect-deploy: prefect-deploy-us

prefect-deploy-us: check-prefect
	PREFECT_WORK_POOL="prompt-extraction-us" $(PYTHON) -m automated_extraction.workflows.register_deployments --deploy-local --region us

prefect-deploy-uk: check-prefect
	PREFECT_WORK_POOL="prompt-extraction-uk" $(PYTHON) -m automated_extraction.workflows.register_deployments --deploy-local --region uk

prefect-worker: check-prefect
	$(PYTHON) -m prefect worker start --pool "$(PREFECT_WORK_POOL)"

prefect-worker-uk: check-prefect
	$(PYTHON) -m prefect worker start --pool "prompt-extraction-uk"

prefect-list: check-prefect
	$(PYTHON) -m automated_extraction.workflows.register_deployments --list

deploy-worker-us:
	fly deploy -a prompt-extractor-us -c fly.yaml

deploy-worker-uk:
	fly deploy -a prompt-extractor-uk -c fly-uk.yaml

validate:
	$(PYTHON) -m compileall automated_extraction

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	rm -f .coverage coverage.xml bandit-report.json
	@echo "Cleaned caches and coverage artifacts"
