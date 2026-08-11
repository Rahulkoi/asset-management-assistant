.PHONY: help install seed verify-data test lint run-api run-ui eval eval-dry clean

PY := ./.venv/bin/python
PIP := ./.venv/bin/pip

help:
	@echo "Asset Management Assistant"
	@echo
	@echo "  make install      create .venv and install dependencies"
	@echo "  make seed         build data/assets.db from the spreadsheet"
	@echo "  make verify-data  assert the database still matches the spreadsheet"
	@echo "  make test         run the test suite (no network, no API key needed)"
	@echo "  make lint         ruff check"
	@echo "  make run-api      start the REST API on :8000 (docs at /docs)"
	@echo "  make run-ui       start the Streamlit chat UI on :8501"
	@echo "  make eval         run the golden evaluation set (needs an API key)"
	@echo "  make eval-dry     validate the evaluation cases without calling the model"

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@echo
	@echo "Next: cp .env.example .env and add your GEMINI_API_KEY, then 'make seed'."

seed:
	$(PY) -m assistant.db.seed

verify-data:
	$(PY) -m assistant.db.seed --verify

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src/ tests/ evals/

run-api:
	$(PY) -m uvicorn assistant.api.app:app --reload --port 8000

run-ui:
	$(PY) -m streamlit run src/assistant/ui/streamlit_app.py

eval:
	$(PY) -m evals.runner

eval-dry:
	$(PY) -m evals.runner --dry-run

clean:
	rm -f data/assets.db data/policy_index.json data/traces.jsonl evals/eval_traces.jsonl
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
