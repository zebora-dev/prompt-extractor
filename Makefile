.PHONY: help install login run-batch dry-run check-prefect prefect-server prefect-serve prefect-pool prefect-deploy prefect-worker prefect-list validate clean

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
	@echo "  install         Install Python dependencies"
	@echo "  login           Open ChatGPT login session using the persisted Chrome profile"
	@echo ""
	@echo "Extraction:"
	@echo "  run-batch       Run a batch with BATCH_ID=<uuid>"
	@echo "  dry-run         Load prompts without opening ChatGPT"
	@echo ""
	@echo "Prefect:"
	@echo "  prefect-server  Start local Prefect server"
	@echo "  prefect-serve   Serve the prompt extraction deployment locally"
	@echo "  prefect-pool    Create the process work pool"
	@echo "  prefect-deploy  Register deployments for process workers"
	@echo "  prefect-worker  Start a process worker"
	@echo "  prefect-list    List workflow deployments"
	@echo ""
	@echo "Quality:"
	@echo "  validate        Compile Python modules"
	@echo "  clean           Remove caches"

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e .

login:
	$(PYTHON) -m automated_extraction --login-only

run-batch:
	@test -n "$(BATCH_ID)" || (echo "Set BATCH_ID=<uuid>" && exit 1)
	$(PYTHON) -m automated_extraction --batch-id "$(BATCH_ID)"

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
	PREFECT_WORK_POOL="$(PREFECT_WORK_POOL)" $(PYTHON) -m automated_extraction.workflows.register_deployments --create-pool

prefect-deploy: check-prefect
	PREFECT_WORK_POOL="$(PREFECT_WORK_POOL)" $(PYTHON) -m automated_extraction.workflows.register_deployments --deploy-local

prefect-worker: check-prefect
	$(PYTHON) -m prefect worker start --pool "$(PREFECT_WORK_POOL)"

prefect-list: check-prefect
	$(PYTHON) -m automated_extraction.workflows.register_deployments --list

validate:
	$(PYTHON) -m compileall automated_extraction

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned caches"
