# Production-like mock environment (specs/008).   make -C mock up | down | reset | test | logs | clean
# Needs Docker with Compose, the AWS SAM CLI, and IDP_REPO (the IDP accelerator checkout) so the function package includes the IDP SDK.

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
RUN ?= $(HERE).run
# The interpreter for the host-side tools: the first of these that has PyYAML and requests (mock/requirements.txt), else python3.
# Override: make PY=/path/to/python
PY ?= $(shell for p in python3 python3.13 python3.12 python3.11 /usr/bin/python3 python; do \
  $$p -c 'import yaml, requests' >/dev/null 2>&1 && { echo $$p; exit 0; }; done; echo python3)
COMPOSE := docker compose -f $(HERE)compose.yaml
EDGE := http://127.0.0.1:$(or $(MOCK_EDGE_PORT),4566)/__mock__
WAIT ?= 420
POLL ?= 5

.PHONY: up down reset test logs clean build prepare wait idp-creds deps

deps:
	@$(PY) -c 'import yaml, requests' 2>/dev/null || { echo "ERROR: $(PY) lacks PyYAML or requests. Install the host tools into the Python you use:  $(PY) -m pip install -r $(HERE)requirements.txt   (or pick another interpreter: make PY=/path/to/python)" >&2; exit 2; }

prepare: deps
	@mkdir -p "$(RUN)"
	@bash "$(HERE)sqlserver/certs.sh" "$(RUN)"
	@cd "$(HERE)" && PYTHONPATH="$(HERE)" $(PY) -m edge.prepare "$(RUN)"

build:
	@test -n "$(IDP_REPO)" || { echo "ERROR: set IDP_REPO to the IDP accelerator checkout (the function package needs its SDK)" >&2; exit 2; }
	cd "$(HERE).." && cd src && IDP_REPO="$(IDP_REPO)" sam build

up: prepare
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
