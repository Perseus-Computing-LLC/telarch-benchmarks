SHELL := /bin/bash
CARGO := cargo
PYTHON := $(if $(wildcard /opt/data/work/telarch-suite/.venv/bin/python),/opt/data/work/telarch-suite/.venv/bin/python,python3)
TASKSET := $(shell which taskset 2>/dev/null)
IONICE := $(shell which ionice 2>/dev/null)
# Every program and script runs as in the published runs and scripts/run_once.sh: on CPUs 0-9 (ten of
# the twelve performance-core threads) at nice 15 and the lowest best-effort I/O priority, because the
# host is shared and a run must leave it two performance threads and the disk.
PIN := nice -n 15 $(if $(IONICE),$(IONICE) -c2 -n7,) $(if $(TASKSET),$(TASKSET) -c 0-9,)
# Events the on-disk FileLedger phase of argus_1m_scale appends (one sync_data each). The full million
# took 1,161 s on this host and is kept as a separate single run (reports/step3_argus_file_ledger_1m.json);
# repeated runs and CI use fewer so the phase is bounded by minutes, not by the disk for an hour.
ARGUS_FILE_EVENTS ?= 100000

.PHONY: all build bench check clean vendor-check claims-check drift-check bisect-bench scripts-test

all: build check vendor-check claims-check drift-check scripts-test

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

# The numbers in README.md must equal claims.json. `scripts/claims_check.py --write` regenerates them.
claims-check:
	$(PYTHON) scripts/claims_check.py

# claims.json must not have regressed against the committed baseline of the last release (same host).
drift-check:
	$(PYTHON) scripts/drift_check.py

scripts-test:
	$(PYTHON) -m pytest scripts/tests -q

# Per-commit attribution: two refs built into scratch worktrees and run A B B A, ROUNDS times, under
# $(PIN). Exit 1 if a figure regressed by more than THRESHOLD % beyond REF_A's range.
#   make bisect-bench REF_A=v0.1.0-alpha.9 REF_B=HEAD
REF_A ?= v0.1.0-alpha.9
REF_B ?= HEAD
ROUNDS ?= 3
THRESHOLD ?= 10
bisect-bench:
	$(PYTHON) scripts/bisect_bench.py $(REF_A) $(REF_B) --rounds $(ROUNDS) --threshold-pct $(THRESHOLD)

bench: build
	@echo "================================================================================"
	@echo "                RUNNING REPRODUCIBLE TELARCH BENCHMARK SUITE                    "
	@echo "================================================================================"
	mkdir -p reports reports/raw_csvs
	$(PIN) ./target/release/telarch-benchmarks --json reports/microbenchmarks_phase1.json
	$(PIN) ./target/release/knapsack_cost_curve --json reports/step1_knapsack_cost_curve.json
	$(PIN) ./target/release/adversarial_contention --json reports/step2_adversarial_contention.json
	$(PIN) ./target/release/argus_1m_scale --json reports/step3_argus_1m_scale.json --file-events $(ARGUS_FILE_EVENTS) --file-dir $${TMPDIR:-/tmp}/telarch-benchmarks
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
