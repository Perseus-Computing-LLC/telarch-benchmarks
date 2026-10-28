SHELL := /bin/bash
CARGO := cargo
PYTHON := $(if $(wildcard /opt/data/work/telarch-suite/.venv/bin/python),/opt/data/work/telarch-suite/.venv/bin/python,python3)
TASKSET := $(shell which taskset 2>/dev/null)
# Every program and script runs on CPUs 0-11, as in the published runs
PIN := $(if $(TASKSET),$(TASKSET) -c 0-11,)

.PHONY: all build bench check clean vendor-check

all: build check vendor-check

build:
	CARGO_BUILD_JOBS=4 $(CARGO) build --release --all-targets

check:
	CARGO_BUILD_JOBS=4 $(CARGO) test
	# --no-deps: lint this repository's programs. The vendored crates are released upstream code and
	# are not edited here, so their lint state is upstream's to fix.
	CARGO_BUILD_JOBS=4 $(CARGO) clippy --all-targets -p telarch-benchmarks --no-deps -- -D warnings
	CARGO_BUILD_JOBS=4 $(CARGO) audit

# Each vendored crate's Cargo.toml version must equal the version claims.json names for it.
vendor-check:
	$(PYTHON) scripts/vendor_check.py

bench: build
	@echo "================================================================================"
	@echo "                RUNNING REPRODUCIBLE TELARCH BENCHMARK SUITE                    "
	@echo "================================================================================"
	mkdir -p reports reports/raw_csvs
	$(PIN) ./target/release/telarch-benchmarks --json reports/microbenchmarks_phase1.json
	$(PIN) ./target/release/knapsack_cost_curve --json reports/step1_knapsack_cost_curve.json
	$(PIN) ./target/release/adversarial_contention --json reports/step2_adversarial_contention.json
	$(PIN) ./target/release/argus_1m_scale --json reports/step3_argus_1m_scale.json
	$(PIN) ./target/release/argus_anchored_checkpoints --json reports/gap6_argus_anchored_checkpoints.json
	$(PIN) ./target/release/restricted_payload_test --json reports/restricted_payload.json
	$(PIN) ./target/release/swarm_partition --json reports/swarm_partition.json
	$(PIN) $(PYTHON) scripts/gap1_harpe_quality.py
	$(PIN) $(PYTHON) scripts/gap2_harpe_alternatives.py
	$(PIN) $(PYTHON) scripts/gap3_kibisis_multiproc_crashes.py
	$(PIN) $(PYTHON) scripts/gap4_kibisis_model_check.py
	$(PIN) $(PYTHON) scripts/gap5_supply_chain.py
	$(PIN) $(PYTHON) scripts/gap7_hygiene.py
	TELARCH_PIN='$(PIN)' $(PYTHON) scripts/build_claims.py
	@echo "================================================================================"
	@echo "        BENCHMARKS COMPLETED: claims.json FULLY REGENERATED AND VERIFIED        "
	@echo "================================================================================"

clean:
	$(CARGO) clean
	rm -rf reports/raw_csvs

# Memory evaluations (evals/README.md). These download datasets and call a model API.
EVALS_PY := evals/.venv/bin/python
UV := $(shell command -v uv 2>/dev/null)

.PHONY: evals-setup evals-test evals-fetch evals-smoke

evals-setup:
ifneq ($(UV),)
	cd evals && $(UV) venv .venv --python 3.12 && $(UV) pip install --python .venv/bin/python -e '.[test]'
else
	python3 -m venv evals/.venv && evals/.venv/bin/pip install -e 'evals[test]'
endif

evals-test:
	cd evals && .venv/bin/python -m pytest -q

evals-fetch:
	$(EVALS_PY) -m telarch_evals fetch all

# A few questions of each benchmark with real model calls. Pass the key with EVALS_ARGS='--env-file .env' if needed.
evals-smoke:
	$(EVALS_PY) -m telarch_evals longmemeval --per-type 1 --max-usd 1 $(EVALS_ARGS)
	$(EVALS_PY) -m telarch_evals locomo --conversations 1 --per-conversation 12 --max-usd 1 $(EVALS_ARGS)
	$(EVALS_PY) -m telarch_evals beam --size 100K --max-conversations 1 --per-ability 1 --max-usd 1 $(EVALS_ARGS)
	$(EVALS_PY) -m telarch_evals memoryagentbench --per-dataset 2 --max-usd 2 $(EVALS_ARGS)
