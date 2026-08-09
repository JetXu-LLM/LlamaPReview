PYTHON ?= python3
REPOSITORY ?= JetXu-LLM/LlamaPReview
RELEASE_COMMIT ?= $(shell git rev-parse HEAD)
RELEASE_TAG ?=
SDK_WHEEL ?=
RELEASE_DIR ?= dist/release

.PHONY: test replay compile lint format-check manifest public-contract scan-source \
	audit-dependencies verify package-functions release verify-release

test:
	$(PYTHON) -m unittest discover -s tests/unit -p 'test_*.py'

replay:
	$(PYTHON) scripts/run_replay_corpus.py --suite all

compile:
	$(PYTHON) -m compileall -q -f lambdas scripts tests

lint:
	$(PYTHON) -m ruff check .

format-check:
	$(PYTHON) -m ruff format --check \
		scripts/build_lambda_layer.py \
		scripts/build_lambda_zip.py \
		scripts/build_release_artifacts.py \
		scripts/check_public_contract.py \
		scripts/dependency_inventory.py \
		scripts/lambda_manifest.py \
		scripts/release_contract.py \
		scripts/scan_secrets.py \
		scripts/verify_pipeline_layer_runtime.py \
		scripts/verify_release_artifacts.py \
		tests/unit/test_supply_chain_contracts.py

manifest:
	$(PYTHON) scripts/lambda_manifest.py validate

public-contract:
	$(PYTHON) scripts/check_public_contract.py all

scan-source:
	$(PYTHON) scripts/scan_secrets.py --current --history

audit-dependencies:
	$(PYTHON) -m pip_audit --requirement lambdas/LlamaPReviewPipeline/requirements-layer.lock --disable-pip

verify: compile lint format-check test replay manifest public-contract scan-source

package-functions:
	@test -n "$(RELEASE_DIR)" || (echo "RELEASE_DIR is required" >&2; exit 2)
	$(PYTHON) scripts/build_lambda_zip.py LlamaPReviewWebhookHandler $(RELEASE_DIR)/LlamaPReviewWebhookHandler.zip
	$(PYTHON) scripts/build_lambda_zip.py LlamaPReviewPipeline $(RELEASE_DIR)/LlamaPReviewPipeline.zip

release:
	@test -n "$(SDK_WHEEL)" || (echo "SDK_WHEEL is required" >&2; exit 2)
	$(PYTHON) scripts/build_release_artifacts.py \
		--output-dir "$(RELEASE_DIR)" \
		--sdk-wheel "$(SDK_WHEEL)" \
		--repository "$(REPOSITORY)" \
		--commit "$(RELEASE_COMMIT)" \
		$(if $(RELEASE_TAG),--tag "$(RELEASE_TAG)",)

verify-release:
	$(PYTHON) scripts/verify_release_artifacts.py "$(RELEASE_DIR)" \
		--expected-repository "$(REPOSITORY)" \
		--expected-commit "$(RELEASE_COMMIT)" \
		$(if $(RELEASE_TAG),--expected-tag "$(RELEASE_TAG)",)
