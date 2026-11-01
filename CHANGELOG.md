# Changelog

All notable changes to `telarch-benchmarks` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- evals: every LongMemEval row, paid or screen, says how much of each answer turn the reader prompt holds
  (telarch P10.1, `evals/tests/test_lme_answer_coverage.py`). `evidence.answer_turns` lists each has_answer
  turn (session and turn index, role, characters, `coverage_bp`): the share of its whitespace-normalized
  characters that the exact reader prompt (the string `prompt_sha256` digests) holds in verbatim blocks of at
  least 64 characters, or whole; `evidence.answer_coverage_bp` is their mean. The summary adds
  `answer_coverage_bp` over the non-abstention questions, the count of answer turns whole (10000), partial and
  absent (0), and `answer_share_bp`, covered answer-turn characters per 10000 prompt characters (reported, not
  gated). Round recall credits a round when any part of it is packed, so under `--pair-window` it could not show
  an answer turn cut to a window or left out of the pair its round was found through. Coverage is read from the
  prompt after it is built; retrieval and prompts are unchanged.
- evals: a paid LongMemEval run records what a reader-in-the-loop control band reads (telarch P9.3,
  `evals/tests/test_lme_paid_records.py`). A run started with `--no-cache` stores `config.no_cache: true` in
  its manifest (absent otherwise, so earlier configs and their resumes are unchanged; resume a no-cache run
  with `--no-cache`). Each paid row carries `prompt_sha256`, the digest of the exact reader prompt, computed
  as a retrieval-only screen row computes it (`common.prompt_sha256`), so control runs can be shown to differ
  only in the reader and judge and be matched question by question against a $0 screen. No retrieval or
  prompt change: a retrieval-only screen of the P9 30 ids gives `prompt_sha256` 30/30 equal to 41fdffa's.

### Fixed

- evals: LongMemEval under `--pair-window` / `--window` (telarch P9.1). Three defects of the windowed arms on
  LongMemEval, found by `evals/tests/test_lme_windowed.py`; the default arm is untouched (P8.3 control, 30
  ids, retrieval-only: `prompt_sha256` 30/30 and round recall 8,639 bp as before).
  - `evidence()` compared answer-round uids with the packed items' `<round>#p<k>` / `<round>#<j>` ids, so
    round recall read 0 under windowing. A packed item now counts for the round it was cut from
    (`meta.parent_uid`).
  - The reader got session headers and no text: windowed units carried no `turns`, which is what
    `render_history` renders, and the rendered-budget fit counted that empty rendering (question 6ae235be,
    `--pair-window 192`: a 633-token prompt for 7,762 packed tokens; now 7,901). Windowed units carry the
    turns they packed.
  - `--pair-window` labelled the first message of every pair `user` and kept it whole, so a round that opens
    a session with an assistant message (117 of the 7,558 rounds of those 30 questions) reached the reader
    as the user's, uncut. Each message keeps its own role, and an assistant message over the window is cut
    wherever it sits. BEAM units always open with the user's message: on BEAM 1M (35 conversations, 700
    questions, 29,466 units) both windowed item builders give the same items as before.

## [0.1.0-alpha.11] - 2026-10-31

This release measures kibisis 0.1.0-alpha.12 (K-C: one lease coordinator; the Python `FileLeaseCoordinator`
is the crate's, through the `kibisis_native` extension) with harpe still at 0.1.0-alpha.11 and argus-ledger
at 0.1.0-alpha.8. The 1,000,000-event on-disk FileLedger figure is carried over as before. It is the first
tag whose `gap3_kibisis_multiproc_crashes.py` runs against a post-K-C kibisis, so `bisect_bench` has a
side-A reference again (telarch P7 end battery, receipt 51923, failed on the alpha.10 tag for that reason).

### Added

- `telarch_evals.rule_gate` (B-R3 / B-R4): the benign-replay gate and the density-matched placebo
  counterfactual for the TCI rule block. Seeded benign tool-use tasks (seven families, mechanical checks,
  no judge); arms `control` / `rules` / `placebo` / `prior` paired on the same tasks; pre-registration
  with a digest over tasks, blocks, model, budget and margins that refuses a changed launch; verdicts by
  the pre-registered margin (B-R3 pass/fail vs control, B-R4 content_effect/null/harm vs placebo) with
  paired counts. CLI `telarch-evals rule-gate`; `evals/prereg/` holds banked pre-registrations.
  Shakedown on `upstage/solar-mini4:free` (42 tasks, seed 20261029, 5 pp), all at $0.00 reported:
  v1 (prompt v1) tombstoned at 77/126 rows — the control arm had an empty system message, so its 0/26
  measured prompt shape, not rules. v2 (prompt v2, run `20261029T224349Z-rules-4e380c57`) completed and
  stands as recorded under its own pre-registration: control 85.7 %, rules 73.8 %, placebo 100 % →
  B-R3 fail (−11.9 pp), B-R4 harm (−26.2 pp). Reading its rows found two harness defects, both fixed
  and both now part of the digest: scoring v1 rejected answers missing a closing bracket (the right call
  with a dropped brace scored wrong; scoring v2 repairs up to three), and `patch_after_edit` v1 accepted
  only the patch call, so a model obeying the live "re-read after an edit" rule scored 0/6 for doing what
  the rule says (tasks v2 accepts the re-read too). v3 pre-registration (`97da0d14…`) banked; its run is
  the first the gate reports as a result rather than a shakedown.
  **v3 result** (run `20261029T225339Z-rules-a7cae37c`, 126 rows, $0.00): control 81.0 %, rules 95.2 %,
  placebo 97.6 %. **B-R3 pass** (rules − control = +14.3 pp; paired: rules better on 6 tasks, worse on 0).
  **B-R4 null** (rules − placebo = −2.4 pp; paired 0 better / 1 worse). Reading: the live rule block does
  not cost benign performance, and its lift over control is not a content effect — a same-size neutral
  block gets the same lift (the `memory_budget` family goes 0/6 → 6/6 under *any* guidance block; the
  model emits a one-op answer with no block and the two-op answer with one). The rule block's own content
  is, on this task set and model, neither help nor harm. One reader, one seed; a second model is the next
  replication.
- `telarch_evals.ledger_scenario` (B-R5 / R-6): bitemporal gold derived from a Kibisis audit-journal export
  — `state_at`, `current_at`, `valid_at`, `superseded_by` under transaction time and valid time, seeded
  probes at every recorded boundary, no content in any item, pinned to the journal head hash — and a
  benchmark scenario from the Argus ledger + vault: a redacted event stream (tool, outcome, opaque session
  ref, compression ratio, lifecycle transitions, journal actions; never payload text or raw ids) with
  counts of the roadmap's four pressures (decaying lessons, contradictions, concurrent writers, budget
  pressure) and a canonical digest. `naive_latest` is the built-in baseline (answers every "as of T" with
  the vault's current state); the oracle re-derives from the journal. CLI `telarch-evals ledger-scenario`;
  `evals/scripts/export_kibisis_journal.py` writes the two vault exports (journal + entity index, mode
  600) with the interpreter that has kibisis. First build on the live ledger + vault: 48,081 events,
  3,420 gold items, naive baseline 41.8 % (state_at 47.3 %, current_at 50.9 %, valid_at 21.7 %,
  superseded_by 0 %) — the artifact stays local (keys can be private).

### Changed

- Vendored kibisis 0.1.0-alpha.12 (tag `v0.1.0-alpha.12`, commit `4b23f1b5de0379dc76a3a1cf2b933ee8eaecaab0`).
  `gap3_kibisis_multiproc_crashes.py` benchmarks `kibisis.FileLeaseCoordinator` (the crate's file coordinator,
  through `kibisis_native`): same five scenarios; the SIGKILL scenario reads the `.lease` state files back and
  re-acquires instead of `PRAGMA integrity_check`; waits sit past the whole-second deadline. CI installs
  `kibisis` and `kibisis-native` from the attested release wheels rather than `git+` at the tag.
- Re-measured: three pinned runs at `a1d9052` (`reports/runs/2026-10-31/run1..3`). Figures that moved
  against the alpha.10 baseline, published as measured:
  - `kib_lease_expiry_recovery_ms` 1403 → 1989 ms (+41.8 %): a crate change. The K-C coordinator rounds a
    lease deadline up to the whole second, so a 1 s TTL expires at the next second boundary, not 1000 ms
    after acquisition; recovery after expiry therefore takes between one and two seconds.
  - `harpe_curve_avg_us` 24.88 → 29.47 µs (+18.4 %) and `harpe_curve_harness_us` +17.3 %: host condition,
    not harpe. The vendored harpe is byte-identical to the baseline's (same tag) and the harness is unchanged;
    `synthesize_us` and `harness_overhead_us` moved by the same ratio, and the 1-minute load on the shared
    host was 4.9–5.1 at these runs against 2.3–3.8 at the baseline's. `kib_put_ops` −8.7 % and
    `kib_lease8_round_trips` −9.9 % sit in the same direction for the same reason. Not re-run; the
    m8azn.metal run under the standing approval is the controlled reference for the public figures.

- evals `Meter`: spend per model is the gateway-reported `usage.cost` (USD per request, as OpenAI-compatible
  gateways such as the Nous Portal inference API send it, promos applied) when present, else the price
  table. `--max-usd` sees the reported figure; `spend.json` keeps `reported_usd` per model across resumes;
  `snapshot()` adds `reported_usd` and `priced_by` (`reported` | `table`), and `unpriced_models` lists only
  models with neither. A free model reporting `cost: 0` is priced, at zero. `evals/tests/test_reported_cost.py`.

## [0.1.0-alpha.10] - 2026-10-29

This release measures harpe 0.1.0-alpha.11 and kibisis 0.1.0-alpha.11 with argus-ledger still at
0.1.0-alpha.8. The 1,000,000-event on-disk FileLedger figure is carried over as before (measured against
argus-ledger 0.1.0-alpha.7, not repeated).

### Changed
- Re-vendored harpe 0.1.0-alpha.11 (`97e7080`) and kibisis 0.1.0-alpha.11 (`ec17a51`) from their release tags;
  `claims.json`, the README block and the SBOM come from three sequential runs of `scripts/run_once.sh` on
  2026-10-29 (16:15 to 16:24 UTC) under the same pin on the same shared server (`reports/runs/2026-10-29/`).
  Every pass/fail result is unchanged.
- **Kibisis sequential vault put: 327,550 to 129,316 ops/s (-60%).** This is the cost of kibisis 0.1.0-alpha.11's
  journal format 3 (K-B): `InMemoryMemoryVault` now hashes each audit row with SHA-256 and re-signs the journal
  head with HMAC-SHA256 inside the same locked section on every append, where alpha.10 appended an unsigned row.
  The figure is the ingestion rate *with* audit chaining and the signing is the point; a vault that wants the
  old rate without a signed head does not exist in alpha.11 by design. Lease decisions and the Python
  multi-process results are unchanged beyond their noise floors; harpe moved by at most 5% either way.
- `scripts/drift_check.py` compares against the newest `baselines/v*.json` by version instead of a path fixed in
  the script; `baselines/v0.1.0-alpha.10.json` is this re-measure, so the next release is compared to it. The
  alpha.9 baseline stays for history. Against alpha.9 this commit flags `kib_put_ops` as above and nothing else.

### Fixed
- `scripts/bisect_bench.py`: each run gets its own scratch directory (`$TMPDIR/telarch-bisect/run-*`). Two
  batteries running at once used to share `bisect-<sha>` and force-remove it on start, so one run's
  `cargo build` lost its target directory under the other and the section reported `error` with no regression.

- `argus_1m_scale`: the FileLedger scratch directory defaults to the system temp dir (`TMPDIR` when set) under
  `telarch-benchmarks/`, not a fixed `/opt/data/...` path. A fresh clone on another machine could not create that
  path and the first alpha.9 EC2 attempt failed with EACCES. `--file-dir` still overrides it.

## [0.1.0-alpha.9] - 2026-10-28

This release measures harpe 0.1.0-alpha.10, kibisis 0.1.0-alpha.10 and argus-ledger 0.1.0-alpha.8. The one
exception is the 1,000,000-event on-disk FileLedger figure, which is carried over from 0.1.0-alpha.8 and was
measured against argus-ledger 0.1.0-alpha.7; its claim and README row say so.

### Public figures refreshed (2026-10-28)

- The figures on telarch.ai, perseuscomputing.com, the org profile and the harpe, kibisis and argus READMEs are
  the v0.1.0-alpha.9 medians: five gated runs on 2026-10-28 on an EC2 m8azn.metal-12xl bare-metal server (48
  cores, no other tenants), pinned to CPUs 0-11, in place of the v0.1.0-alpha.8 runs of 2026-10-23 on the same
  server type, plus five runs of the Python multi-process test against the kibisis 0.1.0a10 package in the same
  session. Every count and pass/fail result is unchanged. Slower than the v0.1.0-alpha.8 medians: Harpe median
  call time 1.82 µs for 10 items (from 1.35 µs) and 31.41 µs for 200 (from 25.73 µs); Argus appends 1,063,962 a
  second (from 1,113,765), 0.94 µs each (from 0.90 µs), verification 2,493,261 events a second (from 3,110,589),
  the edit at event 500,000 reached and reported in 196.9 ms (from 155.4 ms), and verification with an anchor
  2,477,526 events a second (from 2,930,802; on telarch.ai only); Kibisis lease decisions at 32 threads 1,661,193
  a second (from 1,744,674) and vault writes 424,281 a second (from 425,264; both on telarch.ai only). All of
  these except the vault writes are outside the range of the earlier five runs. Faster: Harpe's cost-curve call
  17.4 µs (from 29.3 µs) and lease decisions at 16 threads 1,363,299 a second (from 1,307,225, inside the earlier
  range). The medians, ranges and raw runs are on telarch.ai/evidence. The figure block in this README is this
  repository's own host measurement and is not changed by the refresh.

### Changed (re-vendor and re-measurement)

- Vendored harpe 0.1.0-alpha.10 (`v0.1.0-alpha.10`, f0099be), kibisis 0.1.0-alpha.10 (`v0.1.0-alpha.10`, 6c57297)
  and argus-ledger 0.1.0-alpha.8 (`v0.1.0-alpha.8`, 67c7370): each of `harpe/`, `kibisis/` and `argus-ledger/`
  holds Cargo.toml, README.md, LICENSE and `src/` of the tag byte for byte (plus harpe's `tests/*.tsv`, which its
  unit tests read). The releases already use getrandom 0.4, sha2 0.11 and ed25519-dalek 3, so the workspace builds
  with `--offline`; Cargo.lock moves the three versions and adds argus-ledger's serde dependency, already in the
  graph. `cargo test`: 85 passed, 1 ignored (was 63 passed, 1 ignored; the crates carry more unit tests). CI
  installs the Python packages at the same three tags.
- `scripts/run_once.sh` and `make bench` put `nice -n 15 ionice -c2 -n7 taskset -c 0-9` before every program
  and script instead of `taskset -c 0-11`. The host that produces the published runs shares its 20 CPUs and its
  btrfs volume with other work, and a full-priority run on all twelve performance-core threads with a `sync_data`
  per event stalled it. Ten performance threads at low CPU and I/O priority leave the rest of the machine
  responsive; `TELARCH_CPUSET` and `TELARCH_NICE` override the defaults. Each run's `run_env.txt` records
  `cpuset`, `nice` and `ionice` beside the full prefix, and the hardware line in `claims.json` and the README
  reads "`taskset -c 0-9` at nice 15, ionice -c2 -n7".
- `reports/`, `claims.json`, the README block and the SBOM are regenerated from three sequential runs of
  `scripts/run_once.sh` on 2026-10-28 (03:16 to 03:24 UTC, a minute between runs, 1-minute load averages of
  6.34, 7.50 and 4.40 at their starts) at `3068152` on the same shared server, under the placement above;
  `reports/runs/2026-10-28/run1..run3` hold them. Every pass/fail result is unchanged: no split-brain, no
  fencing-token rollback, no stale write accepted, the anchored rewrite is caught, and the 10 simulated nodes
  converge at 0% simulated packet loss and not at 25% or 50% (3,150 and 11,250 messages never arrive). Both the
  crates and the placement changed, so a timing difference is not attributed to one of them; what the numbers
  say: Harpe's 10-item call is 2.05 µs (from 1.24), the 200-item call 45.51 µs (from 29.19) and the synthesize
  call per turn 26.08 µs (from 19.29), while the harness overhead measured beside it, which does not run Harpe, is
  unchanged (27.93 µs, from 28.73); harpe 0.1.0-alpha.10 replaced the quantised knapsack with an exact 0/1 solver
  and changed token and content-word counting. Argus verification is 2,018,883 events/s (from 2,376,785) and
  the tamper trap 234.67 ms (from 190.56); argus-ledger 0.1.0-alpha.8 verifies the timestamp rule and parses every
  line on the walk. In-memory appends are 855,907 events/s (from 864,690) and the 100,000-event on-disk rate
  1,201 events/s (from 1,190). Kibisis lease cycles are 75,819/s with 16 workers (from 54,950) and 25,223/s with
  32 (from 26,088, range 24,859 to 51,917); decisions 1,740,626/s and 1,688,574/s (from 1,511,961 and
  1,662,961); vault writes 327,550/s (from 321,973). Release binaries are 6 to 46 KB larger. The RustSec database
  has 1,380 advisories; the audit finds no vulnerabilities and no warnings.
- The 1,000,000-event FileLedger single run (861 events/s, 1,161 s) is not repeated: the phase takes twenty
  minutes of `sync_data` on the shared disk. `reports/step3_argus_file_ledger_1m.json` and
  `reports/runs/2026-10-28/file_ledger_1m/` stay as measured for 0.1.0-alpha.8 at `ee741e8` against argus-ledger
  0.1.0-alpha.7; the run record now names its crate versions, `build_claims.py` reads them, and the claim's
  statement, limits and `crate_versions` say the figure was measured against argus-ledger 0.1.0-alpha.7 and not
  repeated for 0.1.0-alpha.8. `scripts/vendor_check.py` accepts such a carried-over claim only when its run
  record exists and names the same versions, so a figure may lag the vendored code but not silently.
- README: the clone example names v0.1.0-alpha.9 and the method paragraph says how the programs are placed.

### Fixed (2026-10-28 review repair set)

Each item names the finding in the 2026-10-28 review of this repository. The numbers in README.md and
`claims.json` were re-measured on this host after these changes; see "Changed" below for how.

- F1: `adversarial_contention` divided every `acquire` attempt by elapsed time and called it lease operations per
  second, although about 95% of the attempts were rejections, which are a map probe under a lock. It now reports
  `lease_cycles_per_sec` (successful acquire-work-release cycles), `lease_decisions_per_sec` (every answered
  `acquire`) and `rejection_rate` separately, keeps `throughput_ops_per_sec` only as a deprecated alias of
  decisions, requests a 1 s TTL (the in-process coordinator keeps deadlines in whole seconds and rounds up, so the
  old 50 ms request was stored as 1 s) and says that turnover in the contention scenarios is by explicit release.
  A new scenario abandons a lease and measures how long until another holder can take it. `claims.json` publishes
  `kib_lease{16,32}_cycles`, `kib_lease{16,32}_decisions` and `kib_lease_expiry_recovery_ms`, and retires the
  ops/s claims.
- F5: the 8-thread lease loop in `telarch-benchmarks` held one Mutex guard across acquire, renew and release, so
  the threads ran one at a time and `conflict_rejections` was always 0. Each call now takes the lock on its own;
  the result is labelled "contended lease round-trip" and reports round trips, attempts and rejected conflicts
  (`kib_lease8_round_trips`).
- F4: `knapsack_cost_curve` timed item construction and the baseline token count together with `synthesize`, so
  the per-turn figure was about 2.3x Harpe's own cost. Only `synthesize` is timed as Harpe now; `synthesize_us` and
  `harness_overhead_us` are reported separately (`harpe_curve_avg_us`, `harpe_curve_harness_us`), and the report
  states that the reduction percentage follows from the budget/input ratio on a synthetic workload.
- F3: `swarm_partition` verified each node's own hash chain and reported convergence. It now compares the per-node
  replicated message sets after reconciliation and reports `nodes_converged`, `missing_messages` and
  `identical_head_hash_pairs`; the claim is "audit-chain convergence under packet loss" only when every node holds
  every message, otherwise "per-node chain integrity under packet loss". With one retry per dropped packet the 25%
  and 50% scenarios do not converge, and `claims.json` says so (`swarm_*_converged`, `swarm_*_missing`); the
  convergence claim is retired.
- F6: `argus-ledger/` is the tree of the argus v0.1.0-alpha.7 release (a363bf7), which already uses getrandom
  0.4; it was 0.1.0-alpha.5 with a local port. `make check` and CI lint and format-check this repository's crate
  only (`-p telarch-benchmarks --no-deps`), since the vendored crates are unmodified upstream releases.
  `scripts/vendor_check.py` (`make vendor-check`, also in CI) fails when a vendored crate's version differs from
  the one `claims.json` names.
- F7: the Argus append figure was in-memory with a constant payload and was quoted as if it were a durable rate.
  `argus_1m_scale` now also appends events to the on-disk `FileLedger` (flock and `sync_data` per event,
  `--file-events N`) and reports `argus_file_append_eps`; the in-memory claims are labelled in-memory with the
  payload size, and README shows both. The full million on disk was run once (`argus_file_append_1m_eps`,
  run_count 1, about twenty minutes on this host); the repeated runs append 100,000.
- F2: `claims.json` was built from one run with `run_count` 1 and null median and range. `build_claims.py` now
  reads `reports/runs/<date>/<run>/` (written by `scripts/run_once.sh`) and publishes the median of each numeric
  claim across the latest date's runs with its range and run count. `scripts/claims_check.py`
  (`make claims-check`, also in CI) renders the README results block from `claims.json` and fails when they differ,
  when README names a vendored crate at another version, or when a line outside the block quotes a retired claim's
  value.
- F9: `telarch_evals.runlog.verify()` passed a completion whose `files_sha256` was empty and ignored run
  directories the ledger never completed. It lists the former as `invalid_completion` and the latter under
  `unlisted`, and is not valid while either exists.
- F10: the evals response cache keyed sampled requests (temperature above 0) by request alone, so a repeat run
  replayed the first run's draws. Sampled requests are keyed with a per-run salt recorded in the manifest and
  reused on `--resume`; each manifest records `cache.hits` and `cache.misses`.
- F8: `evals/README.md` shows LoCoMo token F1 beside J, from the `summary.json` of the same runs, with run ids.

### Added

- evals: `--window T` (default 0: off) packs single messages, each cut to its best question-scored window of T
  reader tokens, with Harpe capped at 7,900 tokens. Off by default: on the full BEAM 1M set, `--window 128` scored
  lower than whole-turn packing although it found more of the evidence messages.

### Changed

- CI: every action is pinned to a full commit SHA (the Rust toolchain action followed a branch) and runs on Node 24.
  Dependabot proposes weekly grouped updates for actions, Cargo and the evals' Python dependencies once a release is 7
  days old, and an OpenSSF Scorecard workflow reports to code scanning.
- Cargo: ed25519-dalek 3, sha2 0.11 and getrandom 0.4, with the vendored argus-ledger ported as in argus
  (`getrandom::fill` replaces `getrandom::getrandom`); its MSRV rises from 1.85 to 1.88, which sha2 0.11 requires.
  The published figures were measured at v0.1.0-alpha.8 and are not re-measured here.
- README: the headline figures are the v0.1.0-alpha.8 medians now on telarch.ai/evidence: five gated runs on
  2026-10-23 on an EC2 m8azn.metal-12xl bare-metal server (48 cores, no other tenants), pinned to CPUs 0-11, in
  place of the v0.1.0-alpha.7 runs on the same server type. Harpe 0.1.0-alpha.7 is faster: 1.35 µs for 10 items
  (from 1.61 µs) and 25.73 µs for 200 (from 26.03 µs). Its 200 items now count 6,800 tokens, so fitting them to
  1,500 removes 78.0% of the tokens (from 69.5%). The other counts and pass/fail results are unchanged. Kibisis
  and Argus are the same releases as before; three Argus timings are slower, each new median inside the range of
  the earlier five runs: verification speed 3,110,589 events a second (from 3,119,603), the edit at event 500,000
  reached and reported in 155.4 ms (from 154.5 ms), and verification speed with an anchor 2,930,802 events a
  second (from 3,069,300; on telarch.ai only). The multi-process lease rows still come from the v0.1.0-alpha.7
  Python runs, because the gap3 script and kibisis are unchanged at v0.1.0-alpha.8. The clone example names
  v0.1.0-alpha.8.
- README: the committed `reports/` and `claims.json` were made with harpe 0.1.0-alpha.7, as `claims.json` records;
  the sentence describing them said 0.1.0-alpha.6.
- evals: pin harpe 0.1.0a7 (was 0.1.0a6). With the default options both versions build the same reader prompts,
  byte for byte, for all 700 BEAM 1M questions.
- evals: pin argus-ledger 0.1.0a5 (was 0.1.0a4). Argus writes only the run ledger: replaying all 700 BEAM 1M
  questions from the response cache under each version gives the same rows, wall-clock timings aside, and a ledger
  that verifies.

### Fixed

- evals: a resumed run counts what its earlier processes spent. `spend.json` in the run directory holds tokens and
  estimated spend per model, with the list prices used and the price table's date; it is rewritten atomically after
  each model call and loaded on `--resume`, so `--max-usd` caps the run as a whole. Before, the meter restarted at
  zero in each process, and a summary covered only the last process.
- evals: the Telarch budget covers the context the reader gets. The selected units are counted as the benchmark
  renders them, separators and headers included; when the rendering runs over, Harpe packs again with the overhead
  set aside, and the least relevant unit is dropped while it is still over. `full-context` drops its oldest units
  the same way. Rows record `joined_tokens_o200k`, and the manifest records `budget_scope`, so a run started before
  this change refuses to resume on it.
- evals: a cached model response is never replaced (`INSERT OR IGNORE`, then read back), and a call that loses a
  race answers with the stored response. A run holds an flock on `<cache>.lock`, so a second run on the same cache
  stops with an error naming the holder's pid. Before, two runs sharing a cache could overwrite each other's stored
  responses.

## [0.1.0-alpha.8] - 2026-10-23

### Changed

- Vendored harpe 0.1.0-alpha.7: `harpe/` holds the files at harpe's `v0.1.0-alpha.7` tag byte for byte, now
  including `harpe/tests/token_vectors.tsv`, which the crate's unit tests read. The Rust crate counts tokens with the
  same rule as the Python package (harpe #6), so Harpe's token counts, and how many items fit a budget, change.
  kibisis 0.1.0-alpha.9 and argus-ledger 0.1.0-alpha.5 are unchanged. CI installs harpe's Python package at
  `v0.1.0-alpha.7`.
- `reports/`, `claims.json` and the SBOM are regenerated by one run of `make bench` at `689cb18` on 2026-10-23 at
  19:43 UTC, on the same shared server, starting at a 1-minute load average of 2.22. Every pass/fail result is
  unchanged, and the contention runs again show no split-brain and no fencing-token rollback. Harpe's counts follow
  the new token rule: Phase 1's 200 items count 6,800 tokens (was 4,900), so trimming them to the 1,500-token budget
  removes 78.0% of the tokens (was 69.5%), and the tier-0 restricted payload's prompt counts 57 tokens (was 51), a
  92.9% reduction (was 93.6%). The Python scripts' Harpe quality results do not change. Besides the timings, these
  moved: the counts that depend on thread and process scheduling, the Argus chain and checkpoint hashes, the release
  binary sizes (each within 56 bytes of before) and the RustSec database (1,351 advisories; the audit finds no
  vulnerabilities and no warnings).
- README: the headline figures are the v0.1.0-alpha.7 medians now on telarch.ai/evidence: five gated runs on
  2026-10-23 on an EC2 m8azn.metal-12xl bare-metal server (48 cores, no other tenants), pinned to CPUs 0-11,
  in place of the v0.1.0-alpha.5 runs on a shared server. Counts, percentages and pass/fail results are
  unchanged. Most timings are 1.18x to 1.50x faster; three are slower: the 10-item Harpe call (1.61 µs, from
  1.58 µs) and the in-memory lease rates with 16 threads (1,291,700 a second, from 1,607,449) and 32 threads
  (1,692,995, from 1,808,081). The clone example names v0.1.0-alpha.7.
- `make bench` runs the Python scripts under the same `taskset` as the Rust programs, on CPUs 0-11, the CPUs the
  published runs used. The Rust programs were pinned to CPUs 0-3 and the Python scripts not at all. The README
  says how the published medians were run, and that `make bench` does not wait for a quiet machine.
- `reports/`, `claims.json` and the SBOM are regenerated by one run of `make bench` at `d9943e9` on 2026-10-23 at
  14:16 UTC, on the same shared server, starting at a 1-minute load average of 3.85. The reports add the fields
  below and give the host as read from the machine (`13th Gen Intel Core i5-13500, 20 logical CPUs`). Every
  pass/fail result is unchanged, and the contention runs again show no split-brain and no fencing-token rollback.
  Besides the timings, these moved: the counts that depend on thread and process scheduling (leases won and
  conflicts blocked under contention), the Argus chain and checkpoint hashes, the release binaries (5 to 15 KB
  larger, with the host reading in `src/host.rs`) and the RustSec database (1,350 advisories; the audit finds no
  vulnerabilities and no warnings). The README figures are the bare-metal medians and do not change.

### Fixed

- Phase 1 (`telarch-benchmarks`) labelled its Harpe scenarios "10 items / 2k context" and "200 items / 40k
  context". It now prints the item count, the items' tokens by the crate's count and the budget, all taken from
  the run: "10 items, 210 tokens, budget 60" and "200 items, 4,900 tokens, budget 1,500". Its report adds
  `input_tokens` to each scenario, and `tamper_edited_event` and `tamper_reported_event` (both 42) beside
  `tamper_detection_us`, which grows with the edited event's position.
- gap4's report carried a typed-in `"miri_status": "configured for CI nightly runner"`. The script does not run
  Miri, so the field is gone. A new `method` field says what the check is: "Python state-machine model mirroring
  spec/kibisis_lease_fencing.tla (not TLC)".

## [0.1.0-alpha.7] - 2026-10-23

### Fixed

- `telarch-benchmarks` and `argus_anchored_checkpoints` read the host once, after their timed work, and print it
  in the closing banner. Since 0.1.0-alpha.6 they read the whole of `/proc/cpuinfo` (about 113 KiB on a 48-core
  machine) for the opening banner, before the first timed loop. The other programs already read it only when
  writing their reports.

## [0.1.0-alpha.6] - 2026-10-23

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
