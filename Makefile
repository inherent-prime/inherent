.DEFAULT_GOAL := help

.PHONY: help setup quickstart env install validate up dev down restart ps logs health doctor bootstrap seed dev-seed check test test-fast require-stack test-integration test-benchmark test-retrieval-eval release-check release-images release-up release-down lint format-check type-check security-check clean graphify-hooks graphify-refresh check-index-consistency reindex-orphaned-document

COMPOSE              ?= docker compose
PUBLIC_API_URL       ?= http://localhost:18000
INGESTION_API_URL    ?= http://localhost:18002

INGESTION_DIR        ?= services/inh-ingestion-svc
PUBLIC_API_DIR       ?= services/inh-public-api-svc
# Shared contracts package (#132 item 4) -- previously wired into neither
# `make test`/`test-fast` nor CI, so a change to a shared constant (e.g.
# DEFAULT_S3_REGION) or the Weaviate-naming derivation was caught by nothing.
CONTRACTS_DIR        ?= services/inh-contracts

PG_CONTAINER         ?= inherent-oss-postgres
PG_USER              ?= postgres
PG_DB                ?= knowledge_base

MONGO_CONTAINER      ?= inherent-oss-mongodb
MONGO_DB             ?= main

DEV_API_KEY          ?= ink_dev_local_key_001
DEV_WORKSPACE_ID     ?= ws_local_001
DEV_USER_ID          ?= local-dev-user
DEV_KEY_NAME         ?= Local Dev Key
DEV_WORKSPACE_NAME   ?= Local Dev Workspace

## help: Show available targets.
help:
	@awk 'BEGIN {printf "\nInherent local development\n\nUsage:\n  make <target>\n\nTargets:\n"} /^## / {if (help == "") help = substr($$0, 4); next} /^[a-zA-Z0-9_.-]+:/ {if (help) {split($$1, target, ":"); desc = help; sub("^[^:]+: ", "", desc); printf "  %-18s %s\n", target[1], desc; help = ""}}' $(MAKEFILE_LIST)

## setup: Create .env if needed and install both service dev environments.
setup: env install

## quickstart: One command from a fresh checkout to a working local stack.
##             Runs env + install, starts Compose and waits for health,
##             bootstraps the dev workspace/API key, then prints next steps.
quickstart: env install
	@echo "==> Starting local stack (this builds images on first run)..."
	@$(COMPOSE) up --build -d --wait
	@$(MAKE) bootstrap
	@echo "==> Checking service readiness..."
	@bash scripts/dev/doctor.sh || true
	@printf '\n========================================\n'
	@printf 'Inherent is up. Next steps:\n'
	@printf '  make health         # check API health endpoints\n'
	@printf '  make logs           # follow stack logs\n'
	@printf '  make down           # stop the stack\n'
	@printf '\nLocal smoke test (upload + search) is in the README Quickstart.\n'
	@printf 'Dev API key: %s   Workspace: %s\n' "$(DEV_API_KEY)" "$(DEV_WORKSPACE_ID)"
	@printf '========================================\n'

## env: Create .env from .env.example when .env is missing.
env:
	@if [ -f .env ]; then \
		echo ".env already exists"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi

## install: Install dev dependencies for both Python services with uv.
install:
	@uv --project $(INGESTION_DIR) sync --extra dev --group dev
	@uv --project $(PUBLIC_API_DIR) sync --extra dev --group dev
	@uv --project $(CONTRACTS_DIR) sync --extra dev --group dev

## validate: Validate local environment settings across both services.
validate: env
	@uv --project $(INGESTION_DIR) run python scripts/validate_env.py

## up: Build and start the full local Docker Compose stack.
up: env
	@$(COMPOSE) up --build

## dev: Start the stack in the background and bootstrap the local workspace + key.
dev: env
	@$(COMPOSE) up --build -d --wait
	@$(MAKE) bootstrap

## down: Stop the local Docker Compose stack.
down:
	@$(COMPOSE) down

## restart: Restart the local Docker Compose stack in the background.
restart:
	@$(COMPOSE) down
	@$(COMPOSE) up --build -d

## ps: Show local Docker Compose service status.
ps:
	@$(COMPOSE) ps

## logs: Follow local Docker Compose logs. Use SVC=name to limit to one service.
logs:
	@if [ -n "$(SVC)" ]; then \
		$(COMPOSE) logs -f $(SVC); \
	else \
		$(COMPOSE) logs -f; \
	fi

## health: Check public API and ingestion API health endpoints.
health:
	@curl -fsS $(PUBLIC_API_URL)/health
	@printf "\n"
	@curl -fsS $(INGESTION_API_URL)/health
	@printf "\n"

## doctor: Check health of every local service and print triage hints on failure.
doctor:
	@bash scripts/dev/doctor.sh

## graphify-hooks: Install the git post-merge hook that refreshes graphify-out/
##                 after every pull/merge (auto AST-only; opt-in Haiku pass for docs).
graphify-hooks:
	@bash scripts/dev/install-graphify-hooks.sh

## graphify-refresh: Refresh the knowledge graph now (foreground, free AST pass).
##                   For semantic doc re-extraction (Claude Code agent on TRUSTED
##                   content) prefix: GRAPHIFY_ALLOW_AGENT_BYPASS=1 make graphify-refresh
graphify-refresh:
	@GRAPHIFY_REFRESH_SYNC=1 bash scripts/dev/graphify-refresh.sh

## bootstrap: Create the local dev workspaces + API keys in BOTH stores
##            (PostgreSQL api_keys and MongoDB workspaces). Local/dev only.
##            Safe to re-run. Seeds TWO principals here: ink_dev_local_key_001
##            in ws_local_001 (the default dev identity) and
##            ink_dev_local_key_002 in ws_local_002 (a separate owner, used by
##            the tenancy isolation E2E). The second is seeded ONLY because
##            DEV_API_KEY is the local default -- run bootstrap.sh directly with
##            your own API_KEY (as production/Hetzner do) and it is skipped
##            unless you pass SEED_PRINCIPAL_B=1.
##            Overrides: API_KEY / KEY_ID / WORKSPACE_ID / USER_ID and the same
##            names with a _B suffix for the second principal. KEY_ID / KEY_ID_B
##            default to empty, which mints a uuid -- pin one only if you want a
##            readable id and never rotate that key's value in place.
bootstrap:
	@API_KEY="$(DEV_API_KEY)" WORKSPACE_ID="$(DEV_WORKSPACE_ID)" \
	 USER_ID="$(DEV_USER_ID)" KEY_NAME="$(DEV_KEY_NAME)" \
	 WORKSPACE_NAME="$(DEV_WORKSPACE_NAME)" \
	 PG_CONTAINER="$(PG_CONTAINER)" PG_USER="$(PG_USER)" PG_DB="$(PG_DB)" \
	 MONGO_CONTAINER="$(MONGO_CONTAINER)" MONGO_DB="$(MONGO_DB)" \
	 bash scripts/dev/bootstrap.sh

## seed: Alias for bootstrap (kept for backward compatibility).
seed: bootstrap

## dev-seed: Alias for bootstrap.
dev-seed: bootstrap

## check: Run validation, lint, formatting, typing, security checks, and tests.
check: validate lint format-check type-check security-check test

## test: Run tests for both services, inh-contracts, and the root-level suite.
test:
	@cd $(INGESTION_DIR) && uv run pytest
	@cd $(PUBLIC_API_DIR) && uv run pytest
	@cd $(CONTRACTS_DIR) && uv run pytest
	@echo "==> root tests/ (repo-level pins, e.g. Postgres init -- #183)"
	@uvx 'pytest==9.0.2' tests/ -q

## test-fast: Fast offline unit profile for both services (no compose/slow/benchmark).
##            inh-contracts and the root suite have no such markers, so they
##            always run in full -- both take well under a second either way,
##            and skipping the root suite here would let a docker-compose.yml
##            init regression through the innermost loop uncaught.
test-fast:
	@cd $(INGESTION_DIR) && uv run pytest -m 'not compose and not slow and not benchmark'
	@cd $(PUBLIC_API_DIR) && uv run pytest -m 'not compose and not slow and not benchmark'
	@cd $(CONTRACTS_DIR) && uv run pytest
	@uvx 'pytest==9.0.2' tests/ -q

## require-stack: Pre-flight guard -- fails fast with an actionable message
##                 if the local Compose stack isn't up. Wired as a
##                 prerequisite on every target below that runs
##                 compose-marked tests, so a missing stack is a loud
##                 failure instead of a silent all-skipped pass (#209).
require-stack:
	@bash scripts/dev/require-stack.sh

## test-integration: Run Compose-backed integration tests (requires a running stack).
##                    Fails if the stack isn't up (require-stack) or if the
##                    suite executes zero tests even though it is (#209) --
##                    a bare pytest exit code cannot tell "24 passed" apart
##                    from "24 skipped"; run-compose-suite.sh checks the
##                    JUnit report directly instead of trusting $?.
test-integration: require-stack
	@bash scripts/dev/run-compose-suite.sh $(PUBLIC_API_DIR) compose test-integration

## test-benchmark: Run Compose-backed latency/throughput benchmarks for both
##                 services (requires a running stack). NOT `pytest -m
##                 benchmark`: a bare `-m benchmark` REPLACES each service's
##                 `-m 'not compose'` addopts default rather than
##                 intersecting with it, so it silently selects only the
##                 compose-marked benchmarks and then all-skips against no
##                 stack (#209) -- this target passes the full "compose and
##                 benchmark" expression instead.
test-benchmark: require-stack
	@bash scripts/dev/run-compose-suite.sh $(PUBLIC_API_DIR) "compose and benchmark" test-benchmark-public-api
	@bash scripts/dev/run-compose-suite.sh $(INGESTION_DIR) "compose and benchmark" test-benchmark-ingestion

## test-retrieval-eval: Run the Compose-backed retrieval-eval hard gate
##                      (requires a running stack). Same `-m` replaces
##                      `addopts` footgun as test-benchmark above (#209) --
##                      passes "compose and retrieval_eval", never a bare
##                      "-m retrieval_eval".
test-retrieval-eval: require-stack
	@bash scripts/dev/run-compose-suite.sh $(PUBLIC_API_DIR) "compose and retrieval_eval" test-retrieval-eval

## release-check: Run the offline release-acceptance suites across both services.
##                Excludes the slow Compose e2e gate (run via integration.yml /
##                `make test-integration`). See docs/maintainers/release_acceptance_matrix.md.
release-check: check
	@echo "==> public-api contract suite"
	@cd $(PUBLIC_API_DIR) && uv run pytest -m contract
	@echo "==> public-api security suite"
	@cd $(PUBLIC_API_DIR) && uv run pytest -m security
	@echo "==> ingestion eval suite"
	@cd $(INGESTION_DIR) && uv run pytest -m eval
	@echo "==> ingestion failure-injection suite"
	@cd $(INGESTION_DIR) && uv run pytest -m failure_injection
	@echo "==> Offline release-acceptance suites passed. Confirm the Compose e2e gate (integration.yml) before tagging."

RELEASE_COMPOSE      ?= docker-compose.release.yml
INHERENT_VERSION     ?= latest

## release-images: Print the steps to publish the service images to GHCR.
##                 Publishing runs in CI (.github/workflows/publish.yml) and
##                 requires human approval — this target documents the flow.
release-images:
	@echo "Images are published by .github/workflows/publish.yml. To cut a release:"
	@echo "  1. Bump versions in the services/*/pyproject.toml that changed, run"
	@echo "     'uv lock --project services/<svc>' for each, and cut CHANGELOG.md."
	@echo "     Full checklist: docs/maintainers/releasing.md"
	@echo "  2. Push a release-candidate tag, then the final tag:"
	@echo "       git tag v<X.Y.Z>-rc1 && git push origin v<X.Y.Z>-rc1   # candidate"
	@echo "       git tag v<X.Y.Z>     && git push origin v<X.Y.Z>       # final"
	@echo "  3. CI builds linux/amd64+arm64, then PAUSES for approval on the"
	@echo "     'release-publish' Environment (Settings -> Environments ->"
	@echo "     Required reviewers). Approve the run in the Actions tab to push:"
	@echo "       ghcr.io/<owner>/ingestion-svc:<X.Y.Z>   (+ :latest on finals)"
	@echo "       ghcr.io/<owner>/public-api-svc:<X.Y.Z>  (+ :latest on finals)"

## release-up: Start the self-contained stack from PUBLISHED images (no build).
release-up:
	@INHERENT_VERSION=$(INHERENT_VERSION) $(COMPOSE) -f $(RELEASE_COMPOSE) up -d --wait

## release-down: Stop the published-image stack and remove its volumes.
release-down:
	@$(COMPOSE) -f $(RELEASE_COMPOSE) down -v

## lint: Run Ruff checks for both services.
lint:
	@cd $(INGESTION_DIR) && uv run ruff check src tests
	@cd $(PUBLIC_API_DIR) && uv run ruff check src tests
	@cd $(CONTRACTS_DIR) && uv run ruff check src tests

## format-check: Check formatting for both services.
format-check:
	@cd $(INGESTION_DIR) && uv run black --check src tests
	@cd $(PUBLIC_API_DIR) && uv run black --check src tests
	@cd $(CONTRACTS_DIR) && uv run black --check src tests

## type-check: Run mypy for services that currently enable it.
type-check:
	@cd $(PUBLIC_API_DIR) && uv run mypy src

## security-check: Run Bandit for services that currently enable it.
security-check:
	@cd $(PUBLIC_API_DIR) && uv run bandit -c pyproject.toml -r src

## clean: Stop the stack and remove local Compose volumes.
clean:
	@$(COMPOSE) down -v

## check-index-consistency: Find processed documents with Postgres chunks but
##                          no Weaviate vectors (#221). Usage:
##                            make check-index-consistency WORKSPACE_ID=ws_abc123
##                            make check-index-consistency ALL_WORKSPACES=1
##                          See docs/maintainers/index-consistency-runbook.md.
check-index-consistency:
	@if [ -n "$(ALL_WORKSPACES)" ]; then \
		uv --project $(PUBLIC_API_DIR) run python scripts/check_index_consistency.py --all-workspaces; \
	elif [ -n "$(WORKSPACE_ID)" ]; then \
		uv --project $(PUBLIC_API_DIR) run python scripts/check_index_consistency.py --workspace-id "$(WORKSPACE_ID)"; \
	else \
		echo "Usage: make check-index-consistency WORKSPACE_ID=<id>  (or ALL_WORKSPACES=1)"; \
		exit 1; \
	fi

## reindex-orphaned-document: Reindex a document flagged by
##                            check-index-consistency straight from its
##                            stored Postgres chunks (#221) -- does NOT
##                            re-fetch/re-extract/re-chunk the source. Usage:
##                              make reindex-orphaned-document DOCUMENT_ID=doc_abc123
reindex-orphaned-document:
	@if [ -z "$(DOCUMENT_ID)" ]; then \
		echo "Usage: make reindex-orphaned-document DOCUMENT_ID=<id>"; \
		exit 1; \
	fi
	@uv --project $(INGESTION_DIR) run python scripts/reindex_orphaned_document.py --document-id "$(DOCUMENT_ID)"
