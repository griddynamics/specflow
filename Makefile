# Local env file. An absolute default so the same file is used from the root and from
# backend/ (which resolves relative paths against its own directory); override with
# `make ENV_FILE=/path/to/file <target>`.
ENV_FILE ?= $(CURDIR)/.env.local

# Non-secret settings read from the env file (names only here; values never printed).
envval = $(shell sed -n 's/^$(1)=//p' "$(ENV_FILE)" 2>/dev/null | tail -n 1 | tr -d '"')
FRONTEND_PORT ?= $(or $(call envval,FRONTEND_PORT),3001)
E2E_BASE_URL ?= $(or $(call envval,E2E_BASE_URL),http://localhost:$(FRONTEND_PORT))

# Optional bundled services (D32): `make up WITH=temporal,langfuse` (either one, or both). `WITH=keycloak`
# adds the local Keycloak of the MCP QA tiers and switches only `mcp` to `oidc`
# (docs/mcp-server/deployment.md#local-stack). Without WITH the stack uses your own Temporal
# (TEMPORAL_ADDRESS, default host.docker.internal:7233 in the containers) and your own Langfuse (LANGFUSE_BASE_URL and keys, empty = no tracing).
# down, ps and logs always cover every profile, so they act on what `up` started.
comma := ,
space := $(empty) $(empty)
empty :=
WITH ?=
WITH_LIST := $(subst $(comma), ,$(WITH))
BAD_WITH := $(filter-out temporal langfuse keycloak,$(WITH_LIST))
ifneq ($(BAD_WITH),)
$(error WITH accepts temporal, langfuse and keycloak (comma separated), got: $(BAD_WITH))
endif

# Local values for the backend run on the host against the local stack (HOST_TARGETS only, so
# unit tests, validation and CI never get a cloud profile they did not ask for). Environment
# variables already set win (`?=`). Only generated secrets live in the env file; see
# `make env-local`. Compose and the e2e cleanup child have the same defaults of their own.
RUSTFS_HOST_PORT ?= $(or $(call envval,RUSTFS_HOST_PORT),19000)
RUSTFS_ACCESS_KEY ?= $(call envval,RUSTFS_ACCESS_KEY)
RUSTFS_SECRET_KEY ?= $(call envval,RUSTFS_SECRET_KEY)
HOST_TARGETS := api worker temporal-check db-upgrade db-revision
$(HOST_TARGETS): export SPECFLOW_CONFIG_FILE ?= $(CURDIR)/config/cloud.local.yaml
$(HOST_TARGETS): export AWS_ENDPOINT_URL_S3 ?= http://localhost:$(RUSTFS_HOST_PORT)
ifneq ($(RUSTFS_ACCESS_KEY),)
$(HOST_TARGETS): export AWS_ACCESS_KEY_ID ?= $(RUSTFS_ACCESS_KEY)
endif
ifneq ($(RUSTFS_SECRET_KEY),)
$(HOST_TARGETS): export AWS_SECRET_ACCESS_KEY ?= $(RUSTFS_SECRET_KEY)
endif
$(HOST_TARGETS): export DATABASE_HOST ?= localhost
$(HOST_TARGETS): export DATABASE_PORT ?= 5433
$(HOST_TARGETS): export DATABASE_NAME ?= specflow
$(HOST_TARGETS): export DATABASE_USER ?= specflow
$(HOST_TARGETS): export DATABASE_AGENT_USER ?= specflow_agent
$(HOST_TARGETS): export NEO4J_URI ?= bolt://localhost:7687
$(HOST_TARGETS): export NEO4J_USER ?= neo4j
$(HOST_TARGETS): export TEMPORAL_ADDRESS ?= localhost:7233
$(HOST_TARGETS): export TEMPORAL_NAMESPACE ?= specflow-local
# What compose passes to the containers as TEMPORAL_ADDRESS (the host default above is not).
COMPOSE_TEMPORAL_ADDRESS := $(if $(filter temporal,$(WITH_LIST)),specflow-temporal:7233,$(if $(filter undefined,$(origin TEMPORAL_ADDRESS)),,$(TEMPORAL_ADDRESS)))

# The local stack (docker-compose.yml). --env-file only fills the ${...} values in it.
# `env` sets the container-side values of the profiles chosen with WITH; with no profile, a
# TEMPORAL_ADDRESS or LANGFUSE_* from your shell or the env file passes through unchanged, and the
# host default above (localhost:7233) is NOT passed to the containers.
LANGFUSE_PUBLIC_KEY_LOCAL = $(or $(call envval,LANGFUSE_PUBLIC_KEY),pk-lf-specflow-local)
LANGFUSE_SECRET_KEY_LOCAL = $(or $(call envval,LANGFUSE_SECRET_KEY),sk-lf-specflow-local)
# The mcp service reads its own compose names (MCP_AUTH_MODE, MCP_OIDC_ISSUER, MCP_AUTH_JWKS_URL,
# MCP_AUTH_DEV_SIGNING_KEY), never the shared AUTH_* and OIDC_* names that api and frontend
# read (they keep dev login). They are cleared from your shell and set only by WITH=keycloak:
# plain `make up` always runs mcp in dev mode.
KEYCLOAK_ISSUER := http://localhost:8180/realms/specflow
MCP_OIDC_ENV := MCP_AUTH_MODE=oidc MCP_OIDC_ISSUER=$(KEYCLOAK_ISSUER) \
	MCP_AUTH_JWKS_URL=http://specflow-keycloak:8080/realms/specflow/protocol/openid-connect/certs MCP_AUTH_DEV_SIGNING_KEY=
COMPOSE_ENV = env -u TEMPORAL_ADDRESS -u MCP_AUTH_MODE -u MCP_OIDC_ISSUER -u MCP_AUTH_JWKS_URL -u MCP_AUTH_DEV_SIGNING_KEY \
	$(if $(filter keycloak,$(WITH_LIST)),$(MCP_OIDC_ENV)) \
	$(if $(COMPOSE_TEMPORAL_ADDRESS),TEMPORAL_ADDRESS=$(COMPOSE_TEMPORAL_ADDRESS)) \
	$(if $(filter langfuse,$(WITH_LIST)),LANGFUSE_BASE_URL=http://specflow-langfuse-web:3000 LANGFUSE_PUBLIC_KEY=$(LANGFUSE_PUBLIC_KEY_LOCAL) LANGFUSE_SECRET_KEY=$(LANGFUSE_SECRET_KEY_LOCAL)) \
	$(if $(WITH_LIST),COMPOSE_PROFILES=$(subst $(space),$(comma),$(strip $(WITH_LIST))))
COMPOSE_BASE := docker compose --env-file $(ENV_FILE) -f docker-compose.yml
COMPOSE := $(COMPOSE_ENV) $(COMPOSE_BASE)
COMPOSE_ALL := env COMPOSE_PROFILES=temporal,langfuse,keycloak $(COMPOSE_BASE)

# Static validation kit (validation/README.md): one target per track, `make validate` runs all.
# Project inputs of each track sit beside the code they describe: backend/ (importlinter.toml,
# pystatic.toml, archkit.toml, cypher-checks.toml, graph-schema.yaml, sqlcheck.toml,
# sql-schema.json, .sqlfluff, .semgrepignore, [tool.deptry] in pyproject.toml) and frontend/.
# `env -u VIRTUAL_ENV`: the kit tools run in the kit's own environments, not a shell's active venv.
VALIDATION := $(CURDIR)/validation
VALIDATE_REPORTS ?= $(CURDIR)/.validation-reports
VALIDATE_TRACKS := python-lint python-scripts python-arch cypher sql typescript-lint typescript-scripts
VALIDATE_TARGETS := $(addprefix validate-,$(VALIDATE_TRACKS))
KIT_ENV := env -u VIRTUAL_ENV

.PHONY: validate validate-list validate-install validate-install-python validate-install-python-clones validate-install-typescript validate-selftest validate-ci-selftest $(VALIDATE_TARGETS)

# Python targets run in backend/ (ENV_FILE is passed down); compose is root-only.
BACKEND_TARGETS := setup lint unit-test check \
	db-upgrade db-revision \
	api worker temporal-check rss-test

.PHONY: $(BACKEND_TARGETS) env-local up down down-volumes logs ps images \
	frontend-setup frontend-dev frontend-lint frontend-build e2e e2e-production e2e-all

$(BACKEND_TARGETS):
	$(MAKE) --no-print-directory -C backend ENV_FILE=$(ENV_FILE) $@

# Writes the env file with random local secrets (Postgres, Neo4j, dev login and session keys,
# RustFS keys, local Keycloak). It never overwrites and never prints a value. On an existing file it
# appends only the missing Keycloak names (the other names belong to initialised volumes).
# `make env-local ENV_FILE=/path` writes there instead.
ENV_LOCAL_ADDED_NAMES := KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_QA_PASSWORD KEYCLOAK_WEB_CLIENT_SECRET
ENV_LOCAL_NAMES := POSTGRES_ADMIN_PASSWORD DATABASE_PASSWORD DATABASE_AGENT_PASSWORD NEO4J_PASSWORD \
	AUTH_DEV_SIGNING_KEY SESSION_SECRET RUSTFS_ACCESS_KEY RUSTFS_SECRET_KEY $(ENV_LOCAL_ADDED_NAMES)

env-local:
	@command -v openssl >/dev/null || { echo "env-local: openssl not found" >&2; exit 1; }; \
	if [ -e "$(ENV_FILE)" ]; then \
		added=""; \
		for name in $(ENV_LOCAL_ADDED_NAMES); do \
			grep -q "^$$name=" "$(ENV_FILE)" && continue; \
			if [ -s "$(ENV_FILE)" ] && [ -n "$$(tail -c 1 "$(ENV_FILE)")" ]; then echo >> "$(ENV_FILE)"; fi; \
			printf '%s=%s\n' "$$name" "$$(openssl rand -hex 32)" >> "$(ENV_FILE)"; \
			added="$$added $$name"; \
		done; \
		if [ -n "$$added" ]; then echo "env-local: appended$$added to $(ENV_FILE)"; \
		else echo "env-local: $(ENV_FILE) exists, left unchanged"; fi; \
		exit 0; \
	fi; \
	umask 077; tmp="$(ENV_FILE).tmp.$$$$"; : > "$$tmp"; \
	for name in $(ENV_LOCAL_NAMES); do \
		case $$name in RUSTFS_ACCESS_KEY) bytes=10;; *) bytes=32;; esac; \
		printf '%s=%s\n' "$$name" "$$(openssl rand -hex $$bytes)" >> "$$tmp"; \
	done; \
	ln "$$tmp" "$(ENV_FILE)" 2>/dev/null && rm -f "$$tmp" || { rm -f "$$tmp"; echo "env-local: $(ENV_FILE) exists, left unchanged"; exit 0; }; \
	echo "env-local: wrote $(ENV_FILE) ($(ENV_LOCAL_NAMES))"; \
	echo "env-local: add AGENT_MODEL, LLM_GATEWAY_URL and LLM_GATEWAY_API_KEY to $(ENV_FILE) (docs/configuration.md)"

# Build and start the stack and wait until all services are healthy: postgres, neo4j, rustfs,
# rustfs-init, api, mcp (dev mode), worker-parse, worker-agents, frontend. `WITH=temporal,langfuse`
# adds the bundled Temporal and Langfuse, `WITH=keycloak` the local Keycloak and switches mcp to
# oidc against it. Logs are JSON on stdout only (`make logs`).
up:
	$(COMPOSE) up -d --build --wait --wait-timeout 600

# Stops and removes the containers. The volumes (databases) stay; use `down-volumes` to drop them.
down:
	$(COMPOSE_ALL) down --remove-orphans

# Also deletes the named volumes: Postgres, Neo4j, Temporal and Langfuse data are LOST. Uses
# the same --env-file as `up`, so the ${...} variables resolve (plain `docker compose down -v`
# fails on the required ones). Asks first; `make down-volumes YES=1` skips the question.
down-volumes:
	@if [ "$(YES)" != "1" ]; then \
		printf 'This deletes all local stack data (databases, graph, Temporal, Langfuse). Type "yes" to continue: '; \
		read answer; [ "$$answer" = "yes" ] || { echo "aborted"; exit 1; }; \
	fi
	$(COMPOSE_ALL) down -v --remove-orphans

logs:
	$(COMPOSE_ALL) logs -f --tail=200

ps:
	$(COMPOSE_ALL) ps

# Build the api, worker and frontend images, then run the image tests against them.
images:
	$(COMPOSE) build api worker-parse frontend
	$(MAKE) --no-print-directory -C backend ENV_FILE=$(ENV_FILE) image-test

# Frontend on the host with live reload (stop the `frontend` container first: same port).
frontend-setup:
	npm --prefix frontend ci

# Explicit host and port: a clash with the container is a bind error, not a
# silent move to another port.
# Node's own env-file parser (no shell `source`: the file holds a `$`) loads the root
# ENV_FILE into npm and so into check-env and next dev. NODE_OPTIONS refuses
# --env-file, hence node wraps npm. Variables already set in the shell win.
# It needs Node 20.6+ and an npm that is a node script; a Volta/asdf/mise shim is refused with a message.
frontend-dev:
	@test -f "$(ENV_FILE)" || { echo "frontend-dev: env file not found: $(ENV_FILE) (copy .env.example to .env.local, or pass ENV_FILE=/path)" >&2; exit 1; }
	@npm_bin="$$(command -v npm)"; \
	head -n 1 "$$npm_bin" | grep -q node || { echo "frontend-dev: $$npm_bin is not a node script (a version-manager shim?). Run instead: set -a; . ./.env.local; set +a; npm --prefix frontend run dev -- --hostname 127.0.0.1 --port $(FRONTEND_PORT)" >&2; exit 1; }; \
	exec node --env-file="$(ENV_FILE)" "$$npm_bin" --prefix frontend run dev -- --hostname 127.0.0.1 --port $(FRONTEND_PORT)

frontend-lint:
	npm --prefix frontend run lint

frontend-build:
	npm --prefix frontend run build

# Playwright against the running stack (the live pack: needs `make up` and spends model budget, so CI never runs it).
e2e:
	E2E_BASE_URL=$(E2E_BASE_URL) npm --prefix frontend run e2e

# The offline browser pack, as the e2e-production job of .github/workflows/validate.yaml runs it: against
# the BUILT app (`make frontend-build` first) and a stub API, never the real backend. SHARD=i/N runs one shard
# (`make e2e-production SHARD=2/4`); with CI set the reporters are list, JUnit and blob (frontend/playwright.reporter.ts).
# e2e-all runs every pack, keeps going after a failure and names the failed ones.
SHARD ?=
E2E_SHARD_ARG := $(if $(SHARD),--shard=$(SHARD))
E2E_PACKS := e2e-production

e2e-production:
	npm --prefix frontend run e2e:production -- $(E2E_SHARD_ARG)

e2e-all:
	@failed=""; \
	for p in $(E2E_PACKS); do \
		echo "######## $$p"; \
		$(MAKE) --no-print-directory $$p SHARD=$(SHARD) || failed="$$failed $$p"; \
	done; \
	if [ -n "$$failed" ]; then echo "e2e-all: FAILED packs:$$failed" >&2; exit 1; fi; \
	echo "e2e-all: all packs passed"

# ---- Validation ---------------------------------------------------------------------------
# make validate                           every track, keeps going after a failure, lists the failed ones
# make validate TRACKS="python-lint sql"  a subset
# make validate-<track>                   one track: python-lint python-scripts python-arch cypher sql typescript-lint typescript-scripts
# make validate-install                   install the kit tools (uv sync / npm install); once, and after a kit update
# make validate-install-python-clones     only the jscpd engine of validate-python-scripts' clones step (Node.js 24)
# make validate-selftest                  the kit's own tests (only after editing a kit rule)
# make validate-ci-selftest               the CI report script's tests (after editing validation/ci)
# KIT_SKIP="mypy deptry" make validate-python-lint   skip steps of python-lint (names: validation/python/lint/README.md)
# The kit is zero-baseline: any finding fails the target. Exit status 0 clean, non-zero findings.
TRACKS ?= $(VALIDATE_TRACKS)

validate:
	@failed=""; \
	for t in $(TRACKS); do \
		echo "######## validate-$$t"; \
		$(MAKE) --no-print-directory validate-$$t || failed="$$failed $$t"; \
	done; \
	if [ -n "$$failed" ]; then echo "validate: FAILED tracks:$$failed" >&2; exit 1; fi; \
	echo "validate: all tracks clean"

validate-list:
	@echo $(VALIDATE_TRACKS)

# backend/importlinter.toml lists real top-level packages (src/ is the import root, hence
# PYTHONPATH=src); MYPYPATH lets mypy resolve them. backend/tests is excluded from mypy by mypy.ini.
validate-python-lint:
	cd backend && $(KIT_ENV) PYTHONPATH=src MYPYPATH=src:examples IMPORTLINTER_CONFIG=importlinter.toml \
		uv run --project $(VALIDATION)/python/lint bash $(VALIDATION)/python/lint/run.sh .

# Two runs: the backend checks (backend/pystatic.toml), then source-ownership over the whole
# repository (pystatic.toml), which only a repository-rooted run can judge. Both always run; the
# target exits with the worse status (0 clean, 1 findings, 2 tool or configuration error).
# The backend run includes `clones` (jscpd over every backend .py file): it runs the TypeScript
# kit's pinned jscpd engine with node (`make validate-install-python-clones`, or validate-install).
validate-python-scripts:
	mkdir -p $(VALIDATE_REPORTS)/pystatic $(VALIDATE_REPORTS)/pystatic-root
	cd $(VALIDATION)/python/scripts && status=0; \
	$(KIT_ENV) PYSTATIC_REPORTS_ROOT=$(VALIDATE_REPORTS)/pystatic UV_PYTHON=$(VALIDATION)/python/scripts/.venv/bin/python \
		uv run python -m pystatic.run --root $(CURDIR)/backend --config $(CURDIR)/backend/pystatic.toml --level all \
		|| status=$$?; \
	$(KIT_ENV) PYSTATIC_REPORTS_ROOT=$(VALIDATE_REPORTS)/pystatic-root UV_PYTHON=$(VALIDATION)/python/scripts/.venv/bin/python \
		uv run python -m pystatic.scripts.source_ownership --root $(CURDIR) --config $(CURDIR)/pystatic.toml \
		|| { code=$$?; [ $$code -le $$status ] || status=$$code; }; \
	exit $$status

# archkit imports the app, so it runs with the backend's own dependencies; --with-editable adds
# archkit for this run only (backend/pyproject.toml and uv.lock stay untouched).
validate-python-arch:
	cd backend && $(KIT_ENV) uv run --frozen --with-editable $(VALIDATION)/python/arch \
		python -m archkit run --config archkit.toml

# Cypher: lint, parity, no-inline-queries, param-names, semgrep and the live cyver / explain steps
# (the kit starts a Neo4j container, so a Docker daemon is needed).
validate-cypher:
	$(KIT_ENV) sh $(VALIDATION)/run-all.sh --project backend --tracks cypher

validate-sql:
	$(KIT_ENV) sh $(VALIDATION)/run-all.sh --project backend --tracks sql

# Frontend: ESLint/Prettier/stylelint/tsc/knip/semgrep, then the token, contrast, CSS and clone
# checks. tsc needs Next's generated types (`npx next typegen`, part of `make frontend-setup` flow).
validate-typescript-lint:
	$(KIT_ENV) node $(VALIDATION)/typescript/lint/run.mjs --project frontend

validate-typescript-scripts:
	$(KIT_ENV) node $(VALIDATION)/typescript/scripts/run.mjs --config frontend/validation.config.json

validate-install: validate-install-python validate-install-typescript

validate-install-python:
	cd $(VALIDATION)/python/lint && $(KIT_ENV) uv sync
	cd $(VALIDATION)/python/scripts && $(KIT_ENV) uv sync --all-extras
	cd $(VALIDATION)/python/arch && $(KIT_ENV) uv sync
	cd $(VALIDATION)/cypher && $(KIT_ENV) uv sync
	cd $(VALIDATION)/sql && $(KIT_ENV) uv sync

# The jscpd engine of the pystatic `clones` step: the TypeScript kit's pinned @jscpd packages
# (validation/typescript/scripts/package-lock.json). validate-install-typescript installs them too.
validate-install-python-clones:
	cd $(VALIDATION)/typescript/scripts && $(KIT_ENV) npm ci --workspaces=false

validate-install-typescript:
	cd $(VALIDATION)/typescript/lint && $(KIT_ENV) npm install --workspaces=false && $(KIT_ENV) uv sync
	cd $(VALIDATION)/typescript/scripts && $(KIT_ENV) npm install --workspaces=false && $(KIT_ENV) uv sync

validate-selftest: validate-ci-selftest
	$(KIT_ENV) sh $(VALIDATION)/run-all.sh --selftest --tracks python-lint,python-scripts,python-arch,cypher,sql,typescript-lint,typescript-scripts

# The CI report script's own tests (validation/ci/test_ci_report.py), in the python-lint kit's pytest.
validate-ci-selftest:
	$(KIT_ENV) uv run --project $(VALIDATION)/python/lint pytest -q -p no:cacheprovider $(VALIDATION)/ci
