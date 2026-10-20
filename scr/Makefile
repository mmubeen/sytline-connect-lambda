# Used by `sam build` (template Metadata: BuildMethod: makefile). No Docker and no local Python 3.13
# needed: dependencies are installed as Linux x86_64 / CPython 3.13 wheels via pip's cross-platform flags.

FUNCTIONS = IntakeFunction StatusFunction WorkerFunction SweeperFunction DirectFunction

.PHONY: build-DepsLayer $(addprefix build-,$(FUNCTIONS))

build-DepsLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	python3 -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)/python" \
		--platform manylinux2014_x86_64 --implementation cp --python-version 3.13 \
		--only-binary=:all: --upgrade
	find "$(ARTIFACTS_DIR)/python" -name __pycache__ -prune -exec rm -rf {} +

# every function ships the same code; the layer supplies the dependencies
$(addprefix build-,$(FUNCTIONS)):
	cp lambda_function.py "$(ARTIFACTS_DIR)/"
	cp -R syteline handlers "$(ARTIFACTS_DIR)/"
	find "$(ARTIFACTS_DIR)" -name __pycache__ -prune -exec rm -rf {} +
