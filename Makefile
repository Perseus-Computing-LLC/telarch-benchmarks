SHELL := /bin/bash
CARGO := cargo
PYTHON := $(if $(wildcard /opt/data/work/telarch-suite/.venv/bin/python),/opt/data/work/telarch-suite/.venv/bin/python,python3)
TASKSET := $(shell which taskset 2>/dev/null)
PIN := $(if $(TASKSET),$(TASKSET) -c 0-3,)

.PHONY: all build bench check clean

all: build check

build:
	CARGO_BUILD_JOBS=4 $(CARGO) build --release --all-targets

check:
	CARGO_BUILD_JOBS=4 $(CARGO) test
	CARGO_BUILD_JOBS=4 $(CARGO) clippy --all-targets -- -D warnings
	CARGO_BUILD_JOBS=4 $(CARGO) audit

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
	$(PYTHON) scripts/gap1_harpe_quality.py
	$(PYTHON) scripts/gap2_harpe_alternatives.py
	$(PYTHON) scripts/gap3_kibisis_multiproc_crashes.py
	$(PYTHON) scripts/gap4_kibisis_model_check.py
	$(PYTHON) scripts/gap5_supply_chain.py
	$(PYTHON) scripts/gap7_hygiene.py
	$(PYTHON) scripts/build_claims.py
	@echo "================================================================================"
	@echo "        BENCHMARKS COMPLETED: claims.json FULLY REGENERATED AND VERIFIED        "
	@echo "================================================================================"

clean:
	$(CARGO) clean
	rm -rf reports/raw_csvs
