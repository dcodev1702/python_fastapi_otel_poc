# =============================================================================
# Foundry Learn Agent - developer shortcuts (GNU make; every target is one documented command)
# Author : dcodev1702 & M365 Copilot / Cowork
# Created: 2026-09-29
#
#   make            lists the targets
#   make lock       resolve requirements/base.txt -> requirements/lock.txt inside the base image (scripts/lock.sh)
#   make test       trace-shape tests: no network, no OpenAI key, no tokens
# =============================================================================
.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE_JAEGER := docker compose -f compose.yaml -f compose.jaeger.yaml
COMPOSE_ASPIRE := docker compose -f compose.yaml -f compose.aspire.yaml
REQS := $(shell [ -f requirements/lock.txt ] && echo requirements/lock.txt || echo requirements/base.txt)
PY := .venv/bin/python
# The image's Python version, read from the Dockerfile's FROM line (python:3.14.7-slim -> 3.14.7)
PY_VERSION := $(shell sed -n 's/^FROM python:\([0-9.]*\).*/\1/p' Dockerfile)

.PHONY: help lock lock-upgrade venv test lint check build up up-jaeger up-aspire down logs

help: ## list the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  %-14s %s\n", $$1, $$2}'

lock: ## resolve requirements/base.txt -> requirements/lock.txt (inside the Dockerfile's base image)
	scripts/lock.sh

lock-upgrade: ## same, but move every package to the newest release the floors allow - then review `git diff`
	scripts/lock.sh --upgrade

venv: ## create or refresh .venv (the image's Python) from the lock plus the dev tools
	@if command -v uv >/dev/null; then \
		uv venv --allow-existing --python $(PY_VERSION) .venv \
		&& uv pip install --python $(PY) -r $(REQS) -r requirements/dev.txt; \
	else \
		python3 -m venv .venv && $(PY) -m pip install --upgrade pip \
		&& $(PY) -m pip install -r $(REQS) -r requirements/dev.txt; \
	fi
	@echo "activate with: source .venv/bin/activate"

test: ## trace-shape tests (pytest, in .venv)
	$(PY) -m pytest -q

lint: ## pylint app.py and the tests with the repo's .pylintrc (in .venv)
	$(PY) -m pylint app.py tests

check: lint test ## lint + test, the same two steps CI runs

build: ## build the image
	docker compose build

up: ## console mode: spans and the [healthz] heartbeat in the container log
	docker compose up --build

up-jaeger: ## Jaeger mode: spans in http://localhost:16686, trace_url in every response
	$(COMPOSE_JAEGER) up --build

up-aspire: ## Aspire Dashboard mode: traces (and later metrics + logs) in http://localhost:18888
	$(COMPOSE_ASPIRE) up --build

down: ## stop and remove everything, whichever override was used
	-$(COMPOSE_JAEGER) down
	-$(COMPOSE_ASPIRE) down

logs: ## follow the API log
	docker compose logs -f api
