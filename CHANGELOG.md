# Changelog

All notable changes to `telarch-benchmarks` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
