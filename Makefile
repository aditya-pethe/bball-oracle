PYTHON ?= .venv/bin/python
NPM ?= npm
EVAL_AGENT ?= graph
EVAL_ARGS ?=

.PHONY: help test test-python test-agent test-pipeline test-web integration test-db eval eval-baseline eval-conversation eval-conversation-control lint typecheck build verify

help:
	@echo "make test          Run all offline/self-contained test suites"
	@echo "make test-agent    Run agent and eval-harness unit tests"
	@echo "make test-pipeline Run ETL tests"
	@echo "make test-web      Run Next.js/Vitest tests"
	@echo "make integration   Run tests requiring an external disposable Postgres"
	@echo "make eval          Run the paid live eval suite (EVAL_AGENT=graph by default)"
	@echo "make eval-baseline Run the paid live zero-shot baseline eval"
	@echo "make eval-conversation         Paid live multi-turn eval (conversation-v0.yaml)"
	@echo "make eval-conversation-control Same suite with no context — the control"
	@echo "make verify        Run tests, typecheck, lint, and production build"

test: test-python test-web

test-python: test-pipeline test-agent

test-agent:
	$(PYTHON) -m pytest agent/tests evals/tests

test-pipeline:
	$(PYTHON) -m pytest tests

test-web:
	$(NPM) --prefix web test

integration: test-db

test-db:
	@test -n "$(BBALL_TEST_ADMIN_DSN)" || (echo "BBALL_TEST_ADMIN_DSN is required and must point at a disposable local Postgres" >&2; exit 1)
	BBALL_TEST_ADMIN_DSN="$(BBALL_TEST_ADMIN_DSN)" $(PYTHON) -m pytest db/tests

eval:
	$(PYTHON) -m evals.run --agent $(EVAL_AGENT) $(EVAL_ARGS)

eval-baseline:
	$(PYTHON) -m evals.run --agent baseline $(EVAL_ARGS)

eval-conversation:
	$(PYTHON) -m evals.run --suite conversation --agent $(EVAL_AGENT) $(EVAL_ARGS)

# The no-context control: every turn answered as if it were the first. The gap
# between this and eval-conversation is what carrying context actually bought.
eval-conversation-control:
	$(PYTHON) -m evals.run --suite conversation --agent baseline $(EVAL_ARGS)

lint:
	$(NPM) --prefix web run lint

typecheck:
	cd web && npx tsc --noEmit

build:
	$(NPM) --prefix web run build

verify: test typecheck lint build
