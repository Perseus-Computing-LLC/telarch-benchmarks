# Changelog

All notable changes to `telarch-benchmarks` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- README: the headline figures are the v0.1.0-alpha.5 medians now on telarch.ai/evidence: five gated,
  pinned runs on 2026-10-22 of the programs built from harpe 0.1.0-alpha.6, kibisis 0.1.0-alpha.9 and
  argus-ledger 0.1.0-alpha.5 (#9). Counts, percentages and pass/fail results are unchanged; the timings
  moved. The clone example names v0.1.0-alpha.5.

### Fixed

- `gap5_supply_chain.py` no longer prints or records a typed-in list of supply-chain controls. The list
  claimed an OpenSSF Scorecard workflow, Dependabot, SHA-256 pinning of every action and release
  attestations; this repository has no Scorecard workflow, Dependabot configuration or release workflow,
  and one action is pinned to a branch. The script reports only what it runs: cargo audit and the SBOM.
  The cargo audit summary records the advisory database's commit and date instead of a placeholder, and
  counts warnings instead of warning kinds.
- `knapsack_cost_curve` and `gap1_harpe_quality.py` no longer print or record dollar projections at assumed
  prices. The overrun line prints the measured share instead of a fixed 0.00%.
- `telarch-benchmarks` describes its tamper check as what it does: a one-byte edit to event 42, reported at
  event 42, in a time that grows with the edit's position (not a single-bit injection caught immediately).
- `gap4_kibisis_model_check.py` describes itself as a Python state-machine model check that mirrors the TLA+
  spec, not a TLA+ exploration or a Loom simulation, and lists only the two invariants it checks.
- `gap2_harpe_alternatives.py` compares Harpe's Python package with the re-implemented trimmers in one
  process. The `harpe_speedup_*` fields, which divided Python times by the Rust crate's time, are removed, and
  the share of a model call uses Harpe Python's 200-item p50.
- The committed reports keep the measurements of the release run (2026-10-22, 04:18 UTC) and drop the fields
  the corrected programs no longer write: dollar projections (step 1, gap1), the Rust-based gap2 ratios and
  NoStaleOverwrites (gap4). gap2's model-call share is recomputed from that run's Harpe Python 200-item p50.
  `gap5_supply_chain.json` and the SBOM come from a new gap5 run. `claims.json` is unchanged.
- Every program and script records the machine it ran on. The `host` field (`hardware` in gap7) and the
  banner held the fixed text `Linux x86_64, Intel Core i5-13500 (20 threads)` on any machine; they now give
  the architecture, CPU model and logical CPU count read from `/proc/cpuinfo` when the program runs, in the
  form the `hardware` field of `claims.json` already used.

## [0.1.0-alpha.5] - 2026-10-22

### Added
- Round-2 memory options for the `telarch` arm: `--counter` (Harpe packs in the reader's o200k tokens by
  default), `--planner any` (one Kibisis any-word BM25 search), `--fill-floor`, `--passthrough-under`,
  `--recency`, `--mmr` and `--bridge`, each recorded in the run manifest.
- `--retrieval-only`: a $0 screen of memory and evidence recall with no model calls, written to
  `$TELARCH_EVALS_HOME/screens`.
- Run manifests record `package_sources`: each library's version and its index release, git commit or
  editable checkout.
- `--no-memory-security`: the `telarch` arm opens encrypted vaults with kibisis `memory_security=False`, so
  SQLCipher's process-wide `cipher_memory_security` stays off; the manifest records `kibisis_memory_security`.
- `--resolve-dates`: the `telarch` arm shows the reader each remembered turn with its relative dates resolved
  against the turn's session date (harpe `resolve_relative_dates`: "yesterday" said on 11 May 2023 reads
  "yesterday [10 May 2023]"). Kibisis stores and searches the turns unchanged. Recall stats count `dated_units`.
- `--surface-conflicts`: when a selected turn denies what another selected turn states ("I've never written
  integration tests" against "I've written integration tests ..."), the reader sees a note after the denial
  quoting the other statement and its date (harpe `find_conflicts`). Recall stats count `conflict_notes`.
- `--cache-mb N`: the `telarch` arm opens each vault with kibisis `cache_mb=N` (0.1.0a9+), a page cache of N MiB.
  SQLCipher decrypts a page each time it is read from disk, so with SQLite's default cache of a few MiB the
  same pages of a large vault are decrypted again on every search. The manifest records `kibisis_cache_mb`.
- `evals/lme_v2/lmev2_relay.py`: a local relay for a LongMemEval-V2 reader behind an HTTPS proxy that drops
  requests idle for 100 seconds. It streams each request and returns the whole answer, gives each question a
  seed derived from its messages so that a retry repeats the same sample, and retries proxy errors. It ran the
  Qwen3.5-9B runs of 2026-10-21 and was kept outside the repository until now; offline tests are in
  `evals/tests/test_lmev2_relay.py`.
- Screen rows record `prompt_sha256`, the SHA-256 of the exact reader prompt (a message list is hashed as
  sorted-key JSON), so comparing two screens shows which questions' prompts a flag changed.

### Changed
- The vendored `harpe/`, `kibisis/` and `argus-ledger/` directories are the current release files, byte for
  byte (`Cargo.toml`, `LICENSE`, `README.md` and the Rust sources): harpe 0.1.0-alpha.6 (`e54904c`), kibisis
  0.1.0-alpha.9 (`26a009c`) and argus-ledger 0.1.0-alpha.5 (`699a8e0`). Harpe and argus-ledger change their
  version strings and READMEs only: their Rust code is the code measured at v0.1.0-alpha.4 (their releases
  since then change the Python packages). Kibisis drops the unused passphrase and envelope types (0.1.0-alpha.5)
  and adds `FileLeaseCoordinator`, a lease coordinator for processes on one machine (0.1.0-alpha.6); no program
  here uses either, and its in-memory lease coordinator is unchanged. CI installs the Python packages at the
  same tags.
- The SBOM names each vendored crate's version: the crates are released separately and no longer share one.
- The harness pins kibisis 0.1.0a9 and harpe 0.1.0a6. The `telarch` arm searches with kibisis
  `search_keys()` (0.1.0a8+), which returns keys and BM25 scores without opening or decrypting entries (the harness
  holds every unit's text), and `--passthrough-under` decides with harpe's `route_by_fit()` in reader
  tokens. Selections and prompts are unchanged. `--cache-mb` needs kibisis 0.1.0a9; `--resolve-dates` and
  `--surface-conflicts` need harpe 0.1.0a6.
- BEAM: every turn in a batch takes its batch's `time_anchor` as its date. BEAM puts the anchor on one turn
  per batch (the first, in every 100K-10M chat), so before, only 90 of the 2,127 turns at 100K had a date for
  `--resolve-dates` to resolve against (350 of 29,466 at 1M); now all of them do (10M: all but the 93 turns of
  its one batch without an anchor). Each turn shows the same text as before (only the anchored turn shows
  `[anchor]`), so without `--resolve-dates` every prompt is byte-identical. With `--surface-conflicts`, a BEAM
  conflict note now gives the date of the turn it quotes.
- `reports/`, `claims.json` and the SBOM are regenerated with the vendored releases above: one run of
  `make bench` at `951c493` on 2026-10-22 at 04:18 UTC, on a shared server, starting at a 1-minute load
  average of 3.12. Every count, percentage and pass/fail result is unchanged; only the timings moved. Two facts
  change with the kibisis source: it has no direct dependencies (`zeroize` went with the passphrase type), and
  the network-code check reads 5 source files (kibisis adds `file_lease.rs`).
- `scripts/build_claims.py` reads every claim value from `reports/` instead of typing it in, so
  `claims.json` always matches the reports beside it. One run gives one value: `run_count` is 1 and
  `median` and `range` are null. The medians and ranges of repeated runs are on telarch.ai/evidence.
  Commit, toolchain, crate versions, hardware and date are read at run time (`scripts/bench_env.py`),
  and `TELARCH_RANGE_CHECK` is gone with the typed ranges.
- `claims.json` facts: the dependency counts and the network-code check are computed from the vendored
  sources. The Kibisis encryption fact now says SQLCipher applies only when `sqlcipher3` is installed
  and that the 600,000-iteration PBKDF2 key is for entry content. The typed test counts are dropped.
  The `retired` list records each withdrawn claim. Other wording fixes: the Harpe 10-item case has 10
  items (not 50), the Argus events carry a 43-byte payload (not 100–200 bytes), and the checkpoint
  signature is made by the benchmark program.
- gap3 runs `PRAGMA integrity_check` after the SIGKILL test and checks that the child died from
  SIGKILL; gap7 computes its hygiene checks and records the real commit; gap1 records the run time;
  gap5 gives the SBOM a valid random serial number.
- README: the headline figures are the v0.1.0-alpha.4 medians shown on telarch.ai/evidence. The README
  released with v0.1.0-alpha.4 showed the v0.1.0-alpha.3 medians (1.72 µs, 36.32 µs, 864,364, 2,567,911
  and 183.6 ms) under the alpha.4 label, and it does list figures, contrary to the entry below.

## [0.1.0-alpha.4] - 2026-10-19

### Changed
- The README names v0.1.0-alpha.4 as the release tag behind the telarch.ai figures and in its clone command.
- The vendored `harpe/`, `kibisis/` and `argus-ledger/` directories are the v0.1.0-alpha.4 release
  files, byte for byte (`Cargo.toml`, `LICENSE`, `README.md` and the Rust sources). Kibisis refuses a
  tombstoned value under every key in its scope and no longer declares the unused `rusqlite`
  dependency; argus-ledger changes doc comments only; harpe only its version. CI installs the
  Python packages at v0.1.0-alpha.4.
- `sbom/telarch-suite-sbom.json` is regenerated from the new `Cargo.lock`, and
  `gap5_supply_chain.py` reads the suite version from `harpe/Cargo.toml` instead of a typed string.
- `reports/` and `claims.json` are not regenerated by this change.
- The README no longer lists figures and points to telarch.ai/evidence instead. The values in
  `claims.json` are typed into `scripts/build_claims.py` from single runs at `ca3dff7`; they are
  not read from the reports in `reports/`.
- Corrections to the 0.1.0 entries below: Gap 4 is a Python breadth-first search over a state
  machine that mirrors `spec/kibisis_lease_fencing.tla`, not a run of the TLA+ model checker;
  Gap 6's Ed25519 checkpoint signing is done by the benchmark program, not by Argus; and
  `claims.json` is not checked against the reports.
- `cargo fmt` (CI failed its format check at v0.1.0-alpha.3, so its later steps never ran), and CI
  now installs the library Python packages for the gap scripts. `gap5_supply_chain.py`
  and `gap7_hygiene.py` use cargo from `PATH` instead of a hard-coded path.
- In CI, a measured value outside its stated range in `build_claims.py` is a warning instead of a
  failure (`TELARCH_RANGE_CHECK=warn`): a shared runner is not the measurement machine, and one run
  measured Ed25519 checkpoint signing at 54.6 µs against a stated range of 30–50 µs.

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
