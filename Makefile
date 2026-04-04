.PHONY: bootstrap lean-build lean-check index dev smoke bench clean test fmt

# Load .env if present
ifneq (,$(wildcard .env))
  include .env
  export
endif

SERVICE_HOST   ?= localhost
ORCHESTRATOR_PORT ?= 8100
LEAN_ENV_PORT     ?= 8101
PROOF_SEARCH_PORT ?= 8102
RETRIEVAL_PORT    ?= 8103
TELEMETRY_PORT    ?= 8104

# ---- Targets -------------------------------------------------------

bootstrap:
	bash infra/bootstrap.sh

lean-build:
	cd lean && lake build

lean-check:
	cd lean && lake build ForgeLean

index:
	python -m services.retrieval.indexer

dev:
	uvicorn services.orchestrator.main:app --host $(SERVICE_HOST) --port $(ORCHESTRATOR_PORT) --reload & \
	uvicorn services.lean_env.main:app     --host $(SERVICE_HOST) --port $(LEAN_ENV_PORT)     --reload & \
	uvicorn services.proof_search.main:app --host $(SERVICE_HOST) --port $(PROOF_SEARCH_PORT) --reload & \
	uvicorn services.retrieval.main:app    --host $(SERVICE_HOST) --port $(RETRIEVAL_PORT)    --reload & \
	uvicorn services.telemetry.main:app    --host $(SERVICE_HOST) --port $(TELEMETRY_PORT)    --reload & \
	wait

smoke:
	bash infra/smoke_test.sh

bench:
	python -m tests.e2e.run_benchmarks

clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .lake/build lean/.lake/build lean/build
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage

test:
	pytest tests/

fmt:
	ruff format .
	ruff check --fix .
