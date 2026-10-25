# Production-like mock environment (specs/008).   make -C mock up | down | reset | test | logs | clean
# Needs Docker with Compose, the AWS SAM CLI, and IDP_REPO (the IDP accelerator checkout) so the function package includes the IDP SDK.

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
RUN ?= $(HERE).run
# The interpreter for the host-side tools: the first of these that has PyYAML and requests (mock/requirements.txt), else python3.
# Override: make PY=/path/to/python
PY ?= $(shell for p in python3 python3.13 python3.12 python3.11 /usr/bin/python3 python; do \
  $$p -c 'import yaml, requests' >/dev/null 2>&1 && { echo $$p; exit 0; }; done; echo python3)
# The compose command: `docker compose` (the plugin) if it works, else the older `docker-compose`, else `docker compose` (whose error then
# says what is missing). Override: make DOCKER_COMPOSE="docker-compose"  (or "podman compose", or a full path); the tests read the same
# value from the MOCK_COMPOSE environment variable.
DOCKER_COMPOSE ?= $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; \
  elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; else echo "docker compose"; fi)
COMPOSE := $(DOCKER_COMPOSE) -f $(HERE)compose.yaml
# The database engine: SQL Server 2022 (x86 only; needs Rosetta on Apple silicon) or Azure SQL Edge (native on ARM, the default there).
# Override: make MOCK_SQL=server|edge, or set MOCK_SQL_IMAGE / MOCK_SQL_PLATFORM directly.
MOCK_SQL ?= $(if $(filter arm64 aarch64,$(shell uname -m)),edge,server)
ifeq ($(MOCK_SQL),edge)
export MOCK_SQL_IMAGE ?= mcr.microsoft.com/azure-sql-edge:latest
export MOCK_SQL_PLATFORM ?= linux/arm64
else
export MOCK_SQL_IMAGE ?= mcr.microsoft.com/mssql/server:2022-latest
export MOCK_SQL_PLATFORM ?= linux/amd64
endif
EDGE := http://127.0.0.1:$(or $(MOCK_EDGE_PORT),4566)/__mock__
WAIT ?= 420
POLL ?= 5

.PHONY: up down reset test logs clean build prepare wait idp-creds deps containers

# stop early, with the fix, when containers cannot be started (before the slow build)
containers:
	@$(DOCKER_COMPOSE) version >/dev/null 2>&1 || { echo "ERROR: '$(DOCKER_COMPOSE)' does not work: the Compose tool is missing. Install it, e.g. Docker Desktop (includes it), or with Homebrew: brew install docker-compose, then link it as a Docker plugin: mkdir -p ~/.docker/cli-plugins && ln -sfn \"\$$(brew --prefix)/opt/docker-compose/bin/docker-compose\" ~/.docker/cli-plugins/docker-compose   (or just use the separate docker-compose program: make DOCKER_COMPOSE=docker-compose ...)" >&2; exit 2; }
	@docker info >/dev/null 2>&1 || { echo "ERROR: the Docker daemon is not running (docker info fails): start Docker Desktop, or your runtime (for example: colima start)" >&2; exit 2; }

deps:
	@$(PY) -c 'import yaml, requests' 2>/dev/null || { echo "ERROR: $(PY) lacks PyYAML or requests. Install the host tools into the Python you use:  $(PY) -m pip install -r $(HERE)requirements.txt   (or pick another interpreter: make PY=/path/to/python)" >&2; exit 2; }

prepare: deps
	@mkdir -p "$(RUN)"
	@bash "$(HERE)sqlserver/certs.sh" "$(RUN)"
	@cd "$(HERE)" && PYTHONPATH="$(HERE)" $(PY) -m edge.prepare "$(RUN)"

build:
	@test -n "$(IDP_REPO)" || { echo "ERROR: set IDP_REPO to the IDP accelerator checkout (the function package needs its SDK)" >&2; exit 2; }
	cd "$(HERE).." && cd src && IDP_REPO="$(IDP_REPO)" sam build

up: containers prepare
	@if curl -fsS "$(EDGE)/health" >/dev/null 2>&1; then echo "the mock environment is already running (make -C mock down to stop it)"; exit 0; fi
	@test -d "$(HERE)../src/.aws-sam/build/DirectFunction" || $(MAKE) -C "$(HERE)" build
	$(COMPOSE) up -d --build
	@$(MAKE) -C "$(HERE)" wait

wait:
	@echo "waiting for the mock environment (SQL Server can take a while, especially on Apple silicon)..."
	@end=$$(( $$(date +%s) + $(WAIT) )); \
	while :; do \
	  out=$$(curl -fsS "$(EDGE)/health" 2>/dev/null || true); \
	  case "$$out" in *'"ready": true'*) echo "ready"; exit 0;; esac; \
	  if [ $$(date +%s) -ge $$end ]; then echo "ERROR: not ready after $(WAIT)s: $${out:-no answer from $(EDGE)/health}" >&2; \
	    echo "       the service that is not 'up' above is the one to look at: make -C mock logs" >&2; exit 4; fi; \
	  sleep $(POLL); done

down:
	-$(COMPOSE) down -v

reset:
	@curl -fsS -X POST "$(EDGE)/reset"; echo

# one test run at a time: they share the one environment's state
test:
	@mkdir -p "$(RUN)"
	@mkdir "$(RUN)/test.lock" 2>/dev/null || { echo "ERROR: another test run holds $(RUN)/test.lock (remove it if no run is active)" >&2; exit 3; }
	@trap 'rmdir "$(RUN)/test.lock"' EXIT; \
	  $(MAKE) -C "$(HERE)" up && cd "$(HERE).." && MOCK_ENV=1 $(PY) -m pytest tests/mock_env -v

logs:
	$(COMPOSE) logs --tail=100

# real-IDP mode: re-export the developer's temporary credentials from the recorded profile (no restart needed)
idp-creds:
	@cd "$(HERE)" && PYTHONPATH="$(HERE)" $(PY) -m edge.prepare --creds-only && echo "credentials refreshed"

clean: down
	rm -rf "$(RUN)" "$(HERE).idp"
