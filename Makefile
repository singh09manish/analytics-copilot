ifneq (,$(wildcard .env))
include .env
export
endif

# dbt runs with --project-dir/--profiles-dir warehouse/dbt from backend/, so the
# SNOWFLAKE_PRIVATE_KEY_PATH in .env (repo-root-relative) won't resolve there.
# Export an absolute override that profiles.yml prefers when set.
export SNOWFLAKE_PRIVATE_KEY_PATH_ABS := $(CURDIR)/$(SNOWFLAKE_PRIVATE_KEY_PATH)

UV := cd backend && uv

.PHONY: install lint test test-live seed api web check-env load-bronze dbt-run dbt-test ai-library mcp-server

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
