# BENCH review notes (rv1110)

Head under review: d4a4beb (detached, no remote). Reviewer adds files only; nothing is fixed, committed or pushed.

## Restatement

1. Review the repository's own benchmark and claims tooling (scripts/, evals/telarch_evals/, the telarch-benchmarks Rust crate, Makefile, claims_schema.json, .github/workflows/ci.yml) for ways a published number can be wrong, stale or unreproducible without the tooling noticing. The vendored kibisis/, harpe/, argus-ledger/ trees are out of scope.
2. Every confirmed defect becomes one test file `rv1110_<name>.py` in the owning suite (scripts/tests or evals/tests) that asserts the correct behaviour, fails with pytest exit code 1 on this head, and carries a passing `test_control_*`.
3. No existing file is edited, no benchmark is run, no network or paid call is made; while the box is under benchmark load only single test files are run, pinned with `taskset -c 19 nice -n 19`.

## Assumptions not stated in the task

- "Correct behaviour" is taken from what the tool's own docstring, README.md, CONTRIBUTING.md or claims_schema.json says it does; where they are silent, from the plain meaning of the check's name (a check named drift_check must fail on a regression).
- A finding test may call a script's functions in-process or run the script as a subprocess against synthetic files in tmp_path; both count as "importing the code under review" as long as the repository's code is what executes.
- Severity: high = a published number can be wrong or a regression can pass with the gate green; medium = a gate passes vacuously or a verdict is unreliable under a realistic input; low = robustness, hygiene or a misleading message.
- The checked-in reports/, baselines/ and claims.json are data, not code: I do not file findings against their values, only against the code that accepts them.
- .github/workflows/scorecard.yml is read alongside ci.yml because the CI cut asks about pinning and permissions.

## Findings

| id | severity | file:line | effect | test file | exit code observed |
|----|----------|-----------|--------|-----------|--------------------|
| B01 | high | scripts/gap3_kibisis_multiproc_crashes.py:139,151-156,163 (published by scripts/build_claims.py:519-522) | `split_brain_violations` is a counter that is never incremented; the published claim `kib_multiproc_split_brain` = 0 is a constant, not a measurement. A coordinator granting the lease to every process at once still reports 0. | scripts/tests/rv1110_gap3_split_brain_never_counted.py | 1 (1 failed, 1 passed) |
| B02 | high | scripts/build_claims.py:773-785, 811-814 | A bool claim keeps the value of top-level `reports/` with `run_count` N and an empty note even when all N runs say the opposite; `reports/` is never checked against the run directories. | scripts/tests/rv1110_aggregate_bool_ignores_runs.py | 1 (1 failed, 1 passed) |
| B03 | medium | scripts/build_claims.py:761-762, 782-785; scripts/claims_check.py:98-104 | Runs that disagree on a pass/fail claim leave only a `note` in claims.json; the README row is byte-identical to a unanimous pass ("yes \| 3"). | scripts/tests/rv1110_bool_disagreement_not_in_readme.py | 1 (1 failed, 1 passed) |
| B04 | high | scripts/claims_check.py:138-190; scripts/drift_check.py:36-52; scripts/vendor_check.py:49-80; .github/workflows/ci.yml:53-63,73-77; Makefile:17 | Nothing rebuilds claims.json from the committed reports and compares. `harpe_small_p50_us` edited to 1.5 µs (runs give 2.026) with the README re-rendered passes claims-check, drift-check and vendor-check: all three exit 0. | scripts/tests/rv1110_claims_json_not_tied_to_reports.py | 1 (1 failed, 1 passed) |
| B05 | medium | scripts/build_claims.py:106-110, 683, 790-796; scripts/run_once.sh:47 | `benchmark_commit` and each claim's `commit` are the HEAD of the build_claims.py invocation; the `commit=` each run wrote to run_env.txt is never read, so runs of different commits are medianed under one commit. Already true of the committed data: run2 and run3 recorded `851f928-dirty`, claims.json says `851f928`. | scripts/tests/rv1110_run_commit_never_read.py | 1 (2 failed, 1 passed) |
| B06 | medium | scripts/build_claims.py:714-726, 397-416 | The 1M-event run's record is looked up only under the latest runs date; a newer date directory without a hand copy of `file_ledger_1m/` makes `one_m_provenance()` return {} and the 2026-10-28 figure is published with the current commit and crate versions and no `carried_over`. | scripts/tests/rv1110_one_m_provenance_latest_date_only.py | 1 (1 failed, 1 passed) |
| B07 | medium | scripts/build_claims.py:110, 678-684, 798, 810-814; Makefile:54-72 | The hardware line, toolchain, commit and `generated` describe the machine running build_claims.py; the reports' own `host`/`timestamp_utc` are never read. Because reports/runs/2026-11-05/ is committed, `make bench` on any machine writes the dev host's committed medians under that machine's hardware line. | scripts/tests/rv1110_hardware_line_is_the_builders.py | 1 (1 failed, 1 passed) |
| B08 | medium | scripts/drift_check.py:41-44, 96; scripts/bench_figures.py:62-70, 74 | drift-check exits 0 when a baseline figure is absent, null, text or NaN in claims.json, and when claims.json has no claims at all ("missing-in-current" is never flagged). | scripts/tests/rv1110_drift_missing_figure_passes.py | 1 (5 failed, 1 passed) |
| B09 | medium | scripts/drift_check.py:58-61, 40 | Bool claims are left out of every baseline; `argus_anchored_rewrite_caught` or `swarm_0pct_converged` flipping true to false passes drift-check. | scripts/tests/rv1110_drift_bool_claims_unchecked.py | 1 (2 failed, 1 passed) |
| B10 | medium | scripts/drift_check.py:36-52 (stored but unread: :62, :67; promise: :10-12) | drift-check never compares the baseline's hardware line or a figure's unit with the current ones: another host's figures are compared as if same-host; 1.0 ms against a 2.0 µs baseline is "improved". | scripts/tests/rv1110_drift_host_and_unit_ignored.py | 1 (2 failed, 1 passed) |
| B11 | low | scripts/drift_check.py:30 | `latest_baseline()` sorts v0.1.0, v0.1.0-beta.1 and v0.1.0-rc.1 below v0.1.0-alpha.12, so a later baseline would never be the default. No such baseline exists yet. | scripts/tests/rv1110_latest_baseline_release_order.py | 1 (3 failed, 1 passed) |
| B12 | medium | scripts/gap1_harpe_quality.py:27-31; scripts/gap2_harpe_alternatives.py:26-29; scripts/gap3_kibisis_multiproc_crashes.py:29-33; scripts/build_claims.py:108 | The gap scripts import `<repo>/../<lib>/src` (a sibling checkout outside the repository) ahead of anything else and never compare or record its version; the claims are stamped with the vendored version. In this workspace that sibling is kibisis 0.1.0a16 against a vendored 0.1.0a15. | scripts/tests/rv1110_gap_scripts_import_sibling_checkout.py | 1 (3 failed, 3 passed) |
| B13 | medium | scripts/bisect_bench.py:150-154, 165-168, 250 | An arm with no usable figures (no reports, one report of eight absent, or one key absent, since any of these makes `build()` raise and `figures_of` return {}) gives "flagged: none", exit 0. | scripts/tests/rv1110_bisect_arm_without_figures_exits_zero.py | 1 (3 failed, 1 passed) |
| B14 | medium | scripts/bisect_bench.py:117-131 | `run_once` never clears `<worktree>/reports/`: a report the run did not write is copied from the ref's committed reports/ (tracked files) or from the previous run and scored as a fresh sample. | scripts/tests/rv1110_bisect_stale_reports_copied.py | 1 (2 failed, 1 passed) |
| B15 | medium | evals/telarch_evals/bench_longmemeval.py:255-257; bench_locomo.py:95; bench_memoryagentbench.py:245; bench_beam.py:103-104; evals/telarch_evals/runlog.py:72-81 (promise: evals/README.md:154) | A run on `--data FILE` records only the file's base name: no hash of the questions and gold answers that were scored, and `--resume` continues over a file whose contents changed. | evals/tests/rv1110_local_dataset_not_hashed.py | 1 (2 failed, 1 passed) |
| B16 | medium | evals/telarch_evals/runlog.py:72-81, 87-93 (purpose: evals/telarch_evals/memory.py:82-84; evals/README.md:101,154) | A resume compares only `config`; `repo_commit`, `packages` and `package_sources` stay those of the first process, so rows produced under another kibisis/harpe release or harness commit are published under the old ones, unrecorded. | evals/tests/rv1110_resume_keeps_first_process_versions.py | 1 (2 failed, 1 passed) |
| B17 | low | evals/telarch_evals/runlog.py:104, 106-111, 113-125, 127-131 | After a torn last line the resumed process appends its first row onto the fragment; `rows()` then skips the joined line, so a paid, finished answer is dropped from the summary without an error. | evals/tests/rv1110_row_after_torn_line_lost.py | 1 (1 failed, 1 passed) |
| B18 | medium | evals/telarch_evals/llm.py:167-176; evals/telarch_evals/runlog.py:202-204 (promise: evals/README.md:133) | `--max-usd` is compared with a spend that stays $0 for a model with neither a table price nor a gateway-reported cost (the table has 14 OpenAI ids), so the cap never triggers. That the model is listed as unpriced in the summary is documented (llm.py:112); that the cap is void is not. | evals/tests/rv1110_max_usd_ignores_unpriced_models.py | 1 (1 failed, 1 passed) |
| B19 | medium | evals/telarch_evals/llm.py:331-332; evals/telarch_evals/bench_longmemeval.py:295-297; evals/telarch_evals/bench_memoryagentbench.py:291-293 | The "returned no text" guard catches `content: null` but not `""`: an empty judge reply (a reasoning judge given `max_tokens=10`, finish_reason "length") is recorded as `label: false`, status ok; the run is `complete: true` with every such answer counted wrong instead of retried. | evals/tests/rv1110_empty_judge_reply_scored_wrong.py | 1 (2 failed, 1 passed) |
| B20 | low | scripts/build_claims.py:71-86 (declared at :14); .github/workflows/ci.yml:38 | Without the `jsonschema` package, which neither CI, the Makefile nor CONTRIBUTING.md installs, `check()` tests key presence only: `value: null`, `run_count: "3"`, a three-number `range` are written. No script validates a committed claims.json against claims_schema.json. | scripts/tests/rv1110_schema_checked_for_keys_only.py | 1 (4 failed, 1 passed) |

Every finding test was run alone, with the command in "How the tests were run" below; the last column is the pytest exit code and
summary line of the final run of each file on d4a4beb. In every file the `test_control_*` test passes and only the finding assertions fail.
Counts: 3 high (B01, B02, B04), 14 medium, 3 low (B11, B17, B20).

## Suspected, unconfirmed

Code reasoning only; none of these was executed, most because proving them needs a benchmark run, which this review may not start.

- scripts/run_once.sh:52-58 runs whatever binaries are in target/release and never builds (Makefile:54 `bench: build` does). The `commit=`
  it writes at :47 is the checkout's HEAD, not the commit the binaries were built from; a stale target/ directory is measured under a new commit.
- scripts/run_once.sh:47 with scripts/bench_env.py:56-57: run_once marks the tree dirty with a plain `git diff --quiet`, bench_env.commit()
  ignores the OUTPUTS paths. Run 1 rewrites reports/, so runs 2 and 3 of the committed 2026-11-05 set recorded `851f928-dirty` while
  claims.json says `851f928` (the data side of B05). Neither notices untracked build inputs.
- scripts/build_claims.py:749 (`discover_runs`) skips a run directory that lacks any one report without a message, so a run that crashed
  half-way lowers `run_count` silently instead of failing the build. Partly intended: file_ledger_1m/ lives in the same directory.
- scripts/build_claims.py:709 takes the lexical `max()` of every directory name under reports/runs/ as the "latest date"; a directory
  named anything that sorts after the dates (e.g. `scratch`) becomes the published run set.
- scripts/build_claims.py:475-512 derives the swarm claim ids from each scenario's `packet_loss_pct`; `add_claim` (:114-134) overwrites a
  duplicate id without complaint and a missing scenario just leaves the claim out. claims_check then fails only if the README still has the row.
- scripts/gap3_kibisis_multiproc_crashes.py:204 writes `"stale_writes_evading_sink": 0` as a literal, protected only by an `assert` that
  `python -O` removes; the sink being tested is the script's own FencingAwareSink class, not Kibisis code. Sibling of B01.
- scripts/gap3_kibisis_multiproc_crashes.py:88-90 `except Exception: conflicts += 1` counts any failure (import error in the worker,
  permission error, a crash in the lease code) as a lease conflict, i.e. as the mechanism working.
- scripts/bench_figures.py:34-43 `direction()` returns lower-is-better for any id that is neither in the table nor ends in a known
  suffix. A new higher-is-better figure with an unlisted suffix would have its regressions reported as "improved" and never flagged.
  No current claim id is affected as far as I checked (the table at :14-24 lists them).
- scripts/bisect_bench.py: no minimum sample count; `--rounds 1` gives n=2 per arm and a verdict is still printed (the control of B13
  shows n == 2 accepted). The load readings are recorded, never acted on; that is documented at :22-24, so there is no load guard to fail.
- scripts/bisect_bench.py:81-83 `python_for(worktree)` ignores its argument and returns one hard-coded interpreter path
  (`/opt/data/work/telarch-suite/.venv/bin/python`) or sys.executable: both arms run the gap scripts against the same installed
  harpe/kibisis, so the Python-side figures cannot differ between refs (stated in B12's docstring, not tested separately).
- scripts/claims_check.py:74 formats non-µs/ms floats with one decimal: 99.96 % is rendered and compared as "100.0 %".
- scripts/drift_check.py compares against the baseline *range*, and the baseline rolls with every release: a wide range hides a large
  drop of the median, and a 9 % loss per release never trips the 10 % threshold. This is the documented design, so not filed.
- evals/telarch_evals/bench_locomo.py:100-117 opens one memory store per conversation and asks all of its questions against it. If a
  recall changes stored state (access counts, promotions), a question's context depends on the questions asked before it, and a resumed
  run (which skips finished questions) sees a different store than an uninterrupted one. Not traced into TelarchMemory.recall.
- evals/telarch_evals/bench_memoryagentbench.py:293 uses `"yes" in reply` for the judge label. For LongMemEval that is the official
  rule (bench_longmemeval.py:5); I could not compare the MemoryAgentBench use with its upstream script without network access.
- evals/telarch_evals/bench_longmemeval.py:297 accepts any reply containing the letters "yes" ("Yesterday", "not yes") as correct. This
  is LongMemEval's own evaluate_qa.py rule, so it is protocol fidelity, not a harness defect; with max_tokens=10 the exposure is small.

## CI

.github/workflows/ci.yml

- :9-10 `permissions: contents: read` at the top, no job widens it. Correct.
- :18, :21, :27, :84, :87 every action is pinned by commit SHA (dtolnay/rust-toolchain at a commit of master, :21). Correct.
- :23 Rust toolchain pinned to 1.97.1. Correct.
- :37-38 and :93-94 `pip install --upgrade pip`, then pydantic, cryptography, sqlcipher3-binary, pytest, hatchling and `evals[test]`
  with no versions and no hashes. The interpreter environment of the gates is not reproducible. `jsonschema` is absent (B20).
- :40, :43 harpe and argus-ledger are installed from git *tags* (movable) while kibisis and kibisis-native (:41-42) are pinned by
  sha256. The gap figures CI computes are of whatever those tags point to that day.
- :53-60 the three gates read committed files only. They cannot detect a claims.json that does not follow from the reports (B04), a
  claim that vanished or went null (B08), a pass/fail claim that turned false (B09), or a baseline from another host (B10; the step is
  even named "(committed figures, same host)" at :59 and nothing checks the host).
- :63 `python -m pytest scripts/tests -q` collects `test_*.py` only. There is no skip, xfail or importorskip in scripts/tests or
  evals/tests, so nothing is skipped silently. (The rv1110_*.py files of this review are therefore not collected by this step; run
  them by path.)
- :67 `cargo install cargo-audit --locked || true`: the version is unpinned and an install failure is swallowed, but `cargo audit` on
  :68 then fails the step, so the job cannot pass without an audit. Cosmetic.
- :73-77 "Run benchmark execution & claims regeneration" cannot fail on a wrong number. `make bench` (Makefile:54-75) runs the
  programs, then build_claims.py, and stops; claims-check and drift-check ran earlier (:57, :60) against the committed file and are not
  run on the regenerated one. Nothing compares the regenerated claims.json with anything. The step fails only if a program exits non-zero.
- :74-75 the comment says the reports "are not committed". 203 files under reports/ are tracked, 185 of them under reports/runs/.
  Because reports/runs/2026-11-05/ is present on the runner, the regenerated numbers are the committed medians, not the runner's (B07).
- No job has `timeout-minutes`; a hung benchmark holds a runner for the 6-hour default.
- :18, :84 checkout without `persist-credentials: false` (the token is read-only, so low value).

.github/workflows/scorecard.yml

- :11 `permissions: read-all` at workflow level is wider than the one job needs; the job-level block (:17-19) replaces it for that
  job, so the effective grant is `security-events: write` and `id-token: write`. `id-token: write` is unused while
  `publish_results: false` (:28); the comment on :19 says so. Actions are SHA-pinned (:21, :24, :29, :34).

Makefile

- :17 `all` = build check vendor-check claims-check drift-check scripts-test. No target rebuilds claims.json into a scratch file and
  diffs it against the committed one, which is the missing gate behind B04.
- :74 the closing banner of `make bench` prints "claims.json FULLY REGENERATED AND VERIFIED"; no verification step runs after
  build_claims.py on :72 (build_claims.check() is the key-presence check of B20).

## Documentation claims the code does not implement

| claim | where it is made | what the code does |
|-------|------------------|--------------------|
| "Every claim value is read from a report, so claims.json always matches the reports committed beside" it | scripts/build_claims.py:4 | True only at the instant build_claims.py runs; no gate re-derives it (scripts/claims_check.py:138-190, scripts/drift_check.py:36-52, scripts/vendor_check.py:49-80). B04. Pass/fail values come from top-level reports/, not from the runs counted in `run_count` (scripts/build_claims.py:773-785). B02. |
| "Both files must come from the same host for the comparison to mean anything; the baseline records its hardware line" | scripts/drift_check.py:10-12; Makefile:37; .github/workflows/ci.yml:59 | The hardware line is stored (scripts/drift_check.py:62, :67) and never compared (:36-52). B10. |
| "A ref whose programs do not write a report that build_claims expects has that figure recorded as missing, not guessed." | scripts/bisect_bench.py:27-28 | One absent report or key empties the whole arm and the exit code is 0 (:150-154, :250), B13; a report left in the worktree by git or by the previous run is copied as this run's (:117-131), B14. |
| "All figures in `claims.json` must be reproducible within their stated ranges." | CONTRIBUTING.md:7 | `make bench` on another machine returns the committed medians under that machine's hardware line (scripts/build_claims.py:110, :798, :810-814), B07; the commit a figure names is not the commit the runs recorded (:683, :790-796), B05. |
| `make bench` ... "BENCHMARKS COMPLETED: claims.json FULLY REGENERATED AND VERIFIED" | Makefile:74 | No check follows scripts/build_claims.py on Makefile:72. |
| the reports "are not committed" | .github/workflows/ci.yml:74-75 | reports/*.json and reports/runs/2026-11-05/ are tracked (203 files); build_claims.py aggregates them on the runner (scripts/build_claims.py:810-814). |
| a figure may lag the vendored code, but never silently (`carried_over`) | scripts/build_claims.py:371-375; scripts/vendor_check.py:59-61 | The record is found only under the latest runs date; otherwise the claim is stamped with today's commit and versions and vendor_check passes (scripts/build_claims.py:714-726). B06. |
| manifest.json holds "the URL, license and hash of every input" | evals/README.md:154 | With `--data` / `--data-dir` the dataset is not hashed (evals/telarch_evals/bench_longmemeval.py:255-257, bench_locomo.py:95, bench_memoryagentbench.py:245, bench_beam.py:103-104). B15. |
| the manifest names "the versions of Kibisis, Harpe and Argus, the commit of this repository"; `package_sources` exists "so that a run names the exact code it measured" | evals/README.md:154, :101; evals/telarch_evals/memory.py:83-84 | True for the first process of a run only (evals/telarch_evals/runlog.py:72-81, :87-93). B16. |
| "`--max-usd N`: stops starting new questions once the run's estimated spend reaches N" | evals/README.md:133 | The estimate is $0 for an unpriced model, so the cap cannot trigger (evals/telarch_evals/llm.py:167-176). B18. |
| a failed call is "recorded and retried on resume; never counted as a wrong answer" | evals/telarch_evals/runlog.py:195 | An empty judge reply is counted as a wrong answer (evals/telarch_evals/llm.py:331-332). B19. |

README.md:143 does describe the median-of-committed-runs behaviour and says the tools do not check the load average; those two are
documented, not defects. spec/ holds only kibisis_lease_fencing.tla, which I did not review (see "Not checked").

## Checked and found correct

- scripts/claims_check.py:138-190: every README table row is compared with the rendered claim; a README number that differs from
  claims.json fails (the control tests of B03 and B04 exercise this path).
- scripts/drift_check.py + scripts/bench_figures.py:62-70: a numeric figure that moved beyond the threshold outside the baseline range in
  the bad direction is flagged and the exit code is 1 (controls of B08, B10); the tolerance is applied on the correct side: I called
  bench_figures.verdict() with a baseline of 100 [95, 105] at threshold 10 for both directions (80, 94, 100, 106, 120 give regressed /
  outside-range / within-noise / outside-range / improved when higher is better and the mirror image when lower is better), and a zero
  baseline [0, 0] against 1 gives "regressed".
- scripts/bench_figures.py:24: `kib_stale_writes_rejected` is lower-is-better. The id is misleading, but the claim publishes
  `stale_writes_evading_sink` under the statement "Stale writes accepted by fencing-aware sink ..." (scripts/build_claims.py:530-532), so
  the direction is right.
- scripts/vendor_check.py:49-80: a vendored Cargo.toml version that differs from the claim's `crate_versions` fails.
- scripts/build_claims.py aggregate(): for numeric claims the published value is the median of the runs, `range` their min/max and
  `run_count` their number; the run list is sorted, so the result does not depend on directory listing order.
- scripts/build_claims.py check(): refuses hostnames and e-mail addresses in the output, and missing required keys (control of B20).
- scripts/bisect_bench.py: the A B B A order and the per-arm pairing of runs; a slower arm is flagged and exits 1 (control of B13);
  a report the run did write is the one copied (control of B14).
- .github/workflows: SHA pinning and `contents: read` (see CI).
- evals: selection settings (`limit`, `per_type`, `ids`, `types`, conversations, abilities, datasets) are part of the run config, so
  a resume with a different selection is refused (evals/telarch_evals/runlog.py:72-81; control of B16). Lists are sorted before they
  enter the config; bench_beam.py:99 sorts the directory listing; LoCoMo sessions are ordered numerically (bench_locomo.py:44).
- evals: evidence markers (`has_answer`, `answer_session_ids`, LoCoMo `evidence`) live in `Unit.meta` and are used for recall
  statistics only; the stored text and the reader prompt are built from role/content (bench_longmemeval.py:42-47, :63;
  bench_locomo.py:38-58). The gold answer goes to the judge prompt only. I found no path that puts the answer into the context under test.
- evals: an error row is excluded from accuracy, counted in `errors`, and makes the run `complete: false`
  (evals/telarch_evals/common.py:103-123); a `content: null` reply is an error row (control of B19). A LoCoMo judge reply without a
  parseable label is an error, and any label other than exactly "CORRECT" scores 0 (bench_locomo.py:129-135).
- evals: spend survives a resume and the cap counts earlier processes (existing tests/test_integrity.py); a gateway-reported cost
  replaces the table price without double counting (existing tests/test_reported_cost.py). The cache key covers the base URL and the
  full request body, plus a per-run salt for sampled requests (evals/telarch_evals/llm.py:314-318).
- Performance: nothing measured. The Python gates run in well under a second each on the committed data; I found no hot path worth timing
  that did not require running a benchmark.

## Not checked

- The Rust crate telarch-benchmarks/ (the benchmark programs themselves): listed and grepped only. Whether each program measures what
  its claim says (timer placement, warm-up, what is inside the timed region, percentile computation) was not reviewed. No cargo command was run.
- scripts/gap2_harpe_alternatives.py (beyond its import preamble), gap4_kibisis_model_check.py, gap5_supply_chain.py, gap7_hygiene.py
  and spec/kibisis_lease_fencing.tla: not read.
- evals/telarch_evals/memory.py (1,200+ lines: the Telarch, BM25 and mem0 memory systems), cli.py, sources.py, rule_gate.py,
  ledger_scenario.py, bench_beam.py scoring and bench_memoryagentbench.py metrics other than the judge label: read in part or only outlined.
- Anything needing the network (the pinned upstream evaluation scripts, tests marked `network`) or a paid model call.
- scripts/run_once.sh, `make bench`, `make bisect-bench`: read, never executed.

## How the tests were run

From the clone root, one file at a time, pinned and niced:

    taskset -c 19 nice -n 19 env -u KIBISIS_PASSPHRASE -u KIBISIS_PASSPHRASE_FILE -u CI \
      /opt/data/work/telarch-suite/.venv/bin/python -m pytest -q -p no:cacheprovider scripts/tests/rv1110_<name>.py

    PYTHONPATH=evals taskset -c 19 nice -n 19 env -u KIBISIS_PASSPHRASE -u KIBISIS_PASSPHRASE_FILE -u CI \
      /opt/data/work/telarch-suite/.venv/bin/python -m pytest -q -p no:cacheprovider -m "not network" evals/tests/rv1110_<name>.py

- `import telarch_evals` fails in that interpreter without `PYTHONPATH=evals` (the package is not installed there); with it,
  `telarch_evals.__file__` is inside the clone, and every evals control test asserts so. The scripts tests load the scripts by path
  from `Path(__file__)` and assert the loaded module's directory.
- `tiktoken` is not installed in that interpreter, so the evals finding tests use `NoMemory` and fake models and need no tokenizer.
- The file names do not match pytest's default `test_*.py`, so `python -m pytest scripts/tests` and `python -m pytest evals/tests` do
  not collect them; the existing suites are unaffected by their presence.
- After /opt/data/cache/scratch/review-20261110/BATTERY_DONE appeared, each existing suite was run once with CI's invocation
  (plus `-p no:cacheprovider`): `python -m pytest scripts/tests -q` gave 34 passed; `python -m pytest evals/tests -q -m "not network"`
  stopped at collection (exit 2) because `httpx` is not installed in that interpreter (evals/tests/test_lmev2_relay.py:19; CI installs
  it through `evals[test]`). The state of the existing evals tests on this head is therefore not established by this review.
- `git status --short` at the end shows untracked files only: review_notes/, 15 files scripts/tests/rv1110_*.py and 5 files
  evals/tests/rv1110_*.py. No tracked file is modified, nothing is committed.
