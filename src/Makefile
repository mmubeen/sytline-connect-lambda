# Used by `sam build` (template Metadata: BuildMethod: makefile). No Docker and no local Python 3.13
# needed: dependencies are installed as Linux x86_64 / CPython 3.13 wheels via pip's cross-platform flags.

FUNCTIONS = DirectFunction

# The interpreter that runs pip. `python3` first, but an active virtual environment may have none with pip (created with
# --without-pip, or by uv without --seed), so the first of these that has pip is used. Override: make PYTHON=/path/to/python
PYTHON ?= $(shell for p in python3 python3.13 python3.12 python3.11 /usr/bin/python3 python; do \
  $$p -m pip --version >/dev/null 2>&1 && { echo $$p; exit 0; }; done; echo python3)

.PHONY: build-DepsLayer $(addprefix build-,$(FUNCTIONS))

build-DepsLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	$(PYTHON) -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/python" \
		--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --implementation cp --python-version 3.13 \
		--only-binary=:all: --ignore-requires-python --upgrade
	@# --ignore-requires-python: an old host pip checks Requires-Python against the host interpreter, not 3.13, and
	@# then backtracks for minutes; the pinned versions are the ones the SDK was checked against on 3.13
	$(PYTHON) -m pip install -r requirements-idp.txt -t "$(ARTIFACTS_DIR)/python" \
		--platform manylinux2014_x86_64 --implementation cp --python-version 3.13 \
		--only-binary=:all: --ignore-requires-python --upgrade
	@# The IDP SDK and idp_common are first-party packages of the IDP accelerator, NOT on PyPI (the name idp-common
	@# on PyPI belongs to someone else: never install them by name). They are copied from a local checkout.
	@if [ -n "$(IDP_REPO)" ]; then \
		cp -R "$(IDP_REPO)/lib/idp_sdk/idp_sdk" "$(IDP_REPO)/lib/idp_common_pkg/idp_common" "$(ARTIFACTS_DIR)/python/" ; \
	else \
		echo "WARNING: IDP_REPO is not set: the IDP SDK is not packaged, so reserve and save will answer IDP_UNAVAILABLE" ; \
	fi
	find "$(ARTIFACTS_DIR)/python" -name __pycache__ -prune -exec rm -rf {} +

# every function ships the same code; the layer supplies the dependencies
$(addprefix build-,$(FUNCTIONS)):
	cp lambda_function.py "$(ARTIFACTS_DIR)/"
	cp -R syteline handlers "$(ARTIFACTS_DIR)/"
	find "$(ARTIFACTS_DIR)" -name __pycache__ -prune -exec rm -rf {} +

# Production-like mock environment (specs/008, docs/MOCK_ENVIRONMENT.md): these only delegate to mock/Makefile. They are not
# part of `sam build`: nothing under mock/ is packaged into the function.
.PHONY: mock-up mock-down mock-test
mock-up mock-down mock-test:
	$(MAKE) -C ../mock $(patsubst mock-%,%,$@)
