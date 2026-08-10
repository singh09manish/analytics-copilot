ifneq (,$(wildcard .env))
include .env
export
endif

# dbt runs with --project-dir/--profiles-dir warehouse/dbt from backend/, so the
# SNOWFLAKE_PRIVATE_KEY_PATH in .env (repo-root-relative) won't resolve there.
# Export an absolute override that profiles.yml prefers when set.
export SNOWFLAKE_PRIVATE_KEY_PATH_ABS := $(CURDIR)/$(SNOWFLAKE_PRIVATE_KEY_PATH)

# `uv` is on PATH in CI (astral-sh/setup-uv) but a local install puts it in
# ~/.local/bin, which is not on the PATH make inherits from a non-login shell --
# so `make lint` failed locally with "uv: command not found". Prefer whatever is
# on PATH, fall back to the standard install location.
UV_BIN := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
UV := cd backend && $(UV_BIN)

.PHONY: install lint test test-live seed api web check-env load-bronze dbt-run dbt-test ai-library mcp-server aws-plan aws-up aws-down aws-secret aws-smoke

install:
	$(UV) sync

lint:
	$(UV) run ruff check src tests ../data ../scripts ../warehouse ../mcp_server

test:
	$(UV) run pytest

test-live:
	$(UV) run pytest -m live --no-header -q

seed:
	$(UV) run python ../data/seed/generate.py

load-bronze:
	$(UV) run python ../warehouse/load_bronze.py

ai-library:
	$(UV) run python ../warehouse/load_ai_library.py

dbt-run:
	$(UV) run dbt run --project-dir ../warehouse/dbt --profiles-dir ../warehouse/dbt

dbt-test:
	$(UV) run dbt test --project-dir ../warehouse/dbt --profiles-dir ../warehouse/dbt

api:
	$(UV) run uvicorn copilot.api.main:app --reload --port 8000

web:
	cd frontend && npm run dev

check-env:
	$(UV) run python ../scripts/check_env.py

mcp-server:
	$(UV) run python ../mcp_server/server.py

TF := ~/.local/bin/terraform -chdir=$(CURDIR)/infra

aws-plan:
	$(TF) init -input=false
	$(TF) plan

aws-up:
	$(TF) init -input=false
	$(TF) apply -auto-approve
	@echo "App URL: $$($(TF) output -raw app_url)"

aws-secret:
	$(UV) run python ../scripts/aws_bootstrap_secret.py

aws-smoke:
	@test -n "$(ANALYST_PW)" && test -n "$(ADMIN_PW)" || \
		(echo "usage: make aws-smoke ANALYST_PW=... ADMIN_PW=..." && exit 1)
	@$(UV) run python ../scripts/aws_smoke.py "$$($(TF) output -raw app_url)" "$(ANALYST_PW)" "$(ADMIN_PW)"

aws-down:
	$(TF) init -input=false
	$(TF) destroy -auto-approve
