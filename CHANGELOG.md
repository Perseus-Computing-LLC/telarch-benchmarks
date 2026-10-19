# Changelog

All notable changes to `telarch-benchmarks` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- The README no longer lists figures and points to telarch.ai/evidence instead. The values in
  `claims.json` are typed into `scripts/build_claims.py` from single runs at `ca3dff7`; they are
  not read from the reports in `reports/`.
- Corrections to the 0.1.0 entries below: Gap 4 is a Python breadth-first search over a state
  machine that mirrors `spec/kibisis_lease_fencing.tla`, not a run of the TLA+ model checker;
  Gap 6's Ed25519 checkpoint signing is done by the benchmark program, not by Argus; and
  `claims.json` is not checked against the reports.
- `cargo fmt` (CI failed its format check at v0.1.0-alpha.3, so its later steps never ran), and CI
  now installs the library Python packages at v0.1.0-alpha.3 for the gap scripts. `gap5_supply_chain.py`
  and `gap7_hygiene.py` use cargo from `PATH` instead of a hard-coded path.

## [0.1.0] - 2026-10-18

### Added
- Reproducible benchmark harness with `make bench` target regenerating `claims.json`.
- Strict `claims_schema.json` and validated `claims.json` as single source of truth.
- Gap 1: Harpe answer quality benchmark across 50 multi-turn tasks with 95% CIs.
- Gap 2: Harpe latency comparison against LangChain trim_messages and LlamaIndex ChatBuffer.
- Gap 3: Kibisis multi-process crash resilience, SIGSTOP fencing rejection, and SIGKILL WAL recovery.
- Gap 4: Kibisis formal TLA+ model checking specification and exhaustive state-space verification.
- Gap 5: Supply chain security baseline, CycloneDX v1.5 SBOM, and cargo-audit integration.
- Gap 6: Argus Ed25519 signed checkpoints (C2SP note format) and full-rewrite fail test.
- Gap 7: Release binary size profiling, peak RSS profiling, and raw CSV dataset exports.

### Changed
- Removed undocumented `beam_scale_gauntlet`.
- Deleted stale `CLAIMS-AUDIT.md`.
- Scrubbed all personal hostnames and machine names from benchmark outputs and code.
