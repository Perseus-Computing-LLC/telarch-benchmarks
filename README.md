# Telarch Benchmarks

**The programs behind every figure on [telarch.ai](https://telarch.ai).** Benchmarks, stress tests and fault injection for the three Telarch libraries: [Harpe](https://github.com/Perseus-Computing-LLC/harpe), [Kibisis](https://github.com/Perseus-Computing-LLC/kibisis) and [Argus](https://github.com/Perseus-Computing-LLC/argus).

[![CI](https://github.com/Perseus-Computing-LLC/telarch-benchmarks/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Perseus-Computing-LLC/telarch-benchmarks/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Part of Telarch by [Perseus Computing LLC](https://perseuscomputing.com).

## Headline results

The numbers below are rendered from [`claims.json`](claims.json) by `scripts/claims_check.py`; `make claims-check` fails when they drift. **They were measured on the development host** (a desktop i5 described in the block) and are published here so the measurement is reproducible from this repository. The figures Perseus quotes in collateral are the ones on [telarch.ai/evidence](https://telarch.ai/evidence), measured by the same programs on a dedicated bare-metal server with its own run records; where the two differ, the telarch.ai figure is the one of record.

<!-- claims:start -->
Medians of 3 sequential runs of `scripts/run_once.sh` on Linux x86_64, 13th Gen Intel Core i5-13500, 20 logical CPUs; Rust programs and Python scripts run under `taskset -c 0-9` at nice 15, ionice -c2 -n7 (performance-core threads), rustc 1.97.1, `--release (opt-level=3, codegen-units=16)`, with synthetic data, at commit `9783570` (harpe 0.1.0-alpha.11, kibisis 0.1.0-alpha.11, argus-ledger 0.1.0-alpha.8). The range column is [min, max] across the runs; the raw reports of every run are under `reports/runs/2026-10-29/`. The machine was shared and not idle (`run_env.txt` in each run directory records the load average when it started), so these are reproducible-on-this-host numbers, not a quiet-machine ceiling.

| Crate | Claim | Value | Runs | Range | Program |
| :--- | :--- | ---: | :---: | :--- | :--- |
| Harpe | Tokens removed over 100,000 simulated turns relative to unbudgeted accumulation | 78.3 % | 3 | 78.3 to 78.3 | `knapsack_cost_curve` |
| Harpe | Count of simulated agent turns that exceeded the designated 1,200-token ceiling | 0 turns | 3 | 0 to 0 | `knapsack_cost_curve` |
| Harpe | Average Harpe synthesize latency per turn over 100,000 turns (the synthesize call only) | 24.88 µs | 3 | 24.71 to 25.03 | `knapsack_cost_curve` |
| Harpe | Harness overhead per turn over 100,000 turns: building the 34 synthetic items and counting the unbudgeted baseline, not Harpe | 26.9 µs | 3 | 26.7 to 26.94 | `knapsack_cost_curve` |
| Harpe | Harpe selects and packs 10 context items within budget at microsecond median latency | 2.09 µs | 3 | 2.09 to 2.14 | `telarch-benchmarks` |
| Harpe | Harpe selects and packs 200 context items within a 1,500-token budget: median latency | 44.79 µs | 3 | 44.57 to 46.17 | `telarch-benchmarks` |
| Harpe | Key fact retention accuracy under 400-token budget over 50 multi-turn tasks | 100.0 % | 3 | 100.0 to 100.0 | `scripts/gap1_harpe_quality.py` |
| Kibisis | Moments two competing workers held the write lease simultaneously | 0 events | 3 | 0 to 0 | `adversarial_contention` |
| Kibisis | Fencing token regressions observed under adversarial thread contention | 0 events | 3 | 0 to 0 | `adversarial_contention` |
| Kibisis | Successful lease cycles/s (acquire, work, release) with 16 competing workers | 92,513 cycles/s | 3 | 42,958 to 94,895 | `adversarial_contention` |
| Kibisis | Lease decisions/s under contention (in-memory Rust coordinator), 16 competing workers | 1,842,192 decisions/s | 3 | 1,161,031 to 1,862,964 | `adversarial_contention` |
| Kibisis | Successful lease cycles/s (acquire, work, release) with 32 competing workers | 46,110 cycles/s | 3 | 28,039 to 47,301 | `adversarial_contention` |
| Kibisis | Lease decisions/s under contention (in-memory Rust coordinator), 32 competing workers | 1,758,648 decisions/s | 3 | 971,050 to 1,789,175 | `adversarial_contention` |
| Kibisis | Time until an abandoned lease (never released) could be acquired by another holder | 1,403 ms | 3 | 1,008.2 to 1,836.7 | `adversarial_contention` |
| Kibisis | Contended lease round-trip: completed acquire, renew, release cycles/s with 8 threads on 10 resources | 650,518 round trips/s | 3 | 644,520 to 676,138 | `telarch-benchmarks` |
| Kibisis | Sequential vault write ingestion rate with audit chaining | 129,316 ops/s | 3 | 128,779 to 130,199 | `telarch-benchmarks` |
| Kibisis | Split-brain violations across 2 to 32 concurrent OS child processes | 0 events | 3 | 0 to 0 | `scripts/gap3_kibisis_multiproc_crashes.py` |
| Kibisis | Stale writes accepted by fencing-aware sink after pause past TTL | 0 writes | 3 | 0 to 0 | `scripts/gap3_kibisis_multiproc_crashes.py` |
| Argus | In-memory audit event append throughput through the public API with SHA-256 chaining (no disk) | 899,559 events/s | 3 | 838,397 to 915,090 | `argus_1m_scale` |
| Argus | Argus on-disk FileLedger append throughput over 100,000 events, one sync_data per event | 1,255 events/s | 3 | 1,254 to 1,256 | `argus_1m_scale` |
| Argus | Argus on-disk FileLedger append throughput over 1,000,000 events, one sync_data per event, single run, measured against argus-ledger 0.1.0-alpha.7 and not repeated for 0.1.0-alpha.8 | 861 events/s | 1 |  | `argus_1m_scale` |
| Argus | Cryptographic verification throughput verifying 1,000,000 events | 2,112,281 events/s | 3 | 1,966,824 to 2,180,750 | `argus_1m_scale` |
| Argus | Time required to detect and isolate an edit at sequence #500,000 in a 1,000,000 event ledger | 223.95 ms | 3 | 222.08 to 243.58 | `argus_1m_scale` |
| Argus | Anchored checkpoint verification catches full-chain rewrite attacks | yes | 3 |  | `argus_anchored_checkpoints` |
| Argus | Audit-chain convergence under packet loss: every one of 10 simulated nodes holds every message after reconciliation at 0% simulated packet loss | yes | 3 |  | `swarm_partition` |
| Argus | Nodes converged after reconciliation at 25% simulated packet loss (no: the program's loss model drops a fixed set of (message, node) pairs and retries each once with the same fixed rule, so those pairs can never be delivered — a property of the harness, not of Kibisis or Argus, neither of which runs a replication protocol here; what holds is per-node chain integrity, every node's own chain verifying) | no | 3 |  | `swarm_partition` |
| Argus | Messages that never reached a node after reconciliation at 25% simulated packet loss, summed over nodes | 3,150 messages | 3 | 3,150 to 3,150 | `swarm_partition` |
| Argus | Nodes converged after reconciliation at 50% simulated packet loss (no: the program's loss model drops a fixed set of (message, node) pairs and retries each once with the same fixed rule, so those pairs can never be delivered — a property of the harness, not of Kibisis or Argus, neither of which runs a replication protocol here; what holds is per-node chain integrity, every node's own chain verifying) | no | 3 |  | `swarm_partition` |
| Argus | Messages that never reached a node after reconciliation at 50% simulated packet loss, summed over nodes | 11,250 messages | 3 | 11,250 to 11,250 | `swarm_partition` |

Each claim's scope, dataset and limits are in [`claims.json`](claims.json); `make claims-check` fails when this block differs from it.
<!-- claims:end -->

Argus appends are reported twice on purpose. The in-memory rate is `InMemoryLedger` with a constant 43-byte payload and no disk; the FileLedger rates are the same appends to a JSON-L file with `flock` and `sync_data` on every event, and are bounded by the disk, so they are about three orders of magnitude lower. The 1,000,000-event FileLedger figure is a single run (it took about twenty minutes), kept in `reports/step3_argus_file_ledger_1m.json` with its run record in `reports/runs/2026-10-28/file_ledger_1m/`; it was measured for v0.1.0-alpha.8 against the argus-ledger release vendored then (0.1.0-alpha.7) and was not repeated for 0.1.0-alpha.8, which its table row says. The repeated runs append 100,000 events to the file (`ARGUS_FILE_EVENTS`) and were re-measured. Quote the one that matches the deployment.

## Standard Long-Term Memory Evaluations & Competitive Baselines

In addition to crate microbenchmarks, Telarch is evaluated across 6,111 questions on standard public long-term memory benchmarks using official upstream judges and AST-grounded evidence evaluation (see [`evals/README.md`](evals/README.md)):

| Benchmark | Questions | Encrypted Vault (as shipped) | Plaintext Index | Full Context Baseline (`o200k`) | Delta (Shipped vs Full) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **LongMemEval-S** | 500 | **72.4%** | **72.4%** | 52.0% | **+20.4 pts** `[+15.6, +25.0]` |
| **LoCoMo (J)** | 1,540 | **70.8%** *(74.2% fit / 75.3% dates)* | 70.8% | **75.5%** | −4.7 pts *(−1.3 fit tie)* `[-3.0, +0.0]` |
| **BEAM 100K** | 400 | **33.9%** | 34.4% | 31.0% | **+2.9 pts** `[-1.1, +6.7]` |
| **BEAM 500K** | 700 | **36.5%** | 36.3% | 29.5% | **+7.1 pts** `[+4.0, +10.3]` |
| **BEAM 1M** | 700 | **33.8%** | **33.8%** | 25.2% | **+8.6 pts** `[+6.0, +11.3]` |
| **BEAM 10M** | 200 | **26.7%** | **26.7%** | 15.9% | **+10.9 pts** `[+4.9, +16.6]` |
| **MemoryAgentBench** | 2,071 | **45.6%** | **45.6%** | 40.7% | **+4.9 pts** `[+2.4, +7.3]` |
| **Total / Macro Avg** | **6,111** | **45.7%** *(46.1% fit)* | **45.7%** | 38.5% | **+7.2 pts overall** |

### Competitive Memory Architecture Retrieval (LongMemEval-S, N=30)

| System | Storage & Cipher | Session Recall | Round Recall | Security & Invariants |
| :--- | :--- | :---: | :---: | :--- |
| **Telarch (`telarch`)** | **SQLCipher AES-256 (0o600)** | **93.33%** | **86.39%** | Zero plaintext on disk; Argus ledger audit; monotonic fencing leases |
| **BM25 RAG (`bm25-rag`)** | Plaintext SQLite FTS5 | 93.33% | 85.28% | Plaintext SQLite; no leases; no ledger |
| Mem0-style flat facts (`mem0-local`: our emulation of the Mem0 storage pattern, not Mem0's software) | Unencrypted Flat Facts | 84.72% *(−8.61 pts)* | 74.61% *(−11.78 pts)* | Plaintext facts; loses multi-turn chronology; no ledger |

Full methodology, paired bootstrap intervals, and raw runs are published at [telarch.ai/evidence](https://telarch.ai/evidence).

### MemConflict: Temporal State Tracking & Conflict Resolution (3,750 Questions, 30 Personas)

Evaluated under the official `gj12pen` judge rubric (`unsloth/gemma-4-12b-it-NVFP4` with official penalty scoring; generator: `AxionML/Qwen3.5-4B-NVFP4`) on the full 30-persona MemConflict benchmark (3,750 total questions):

| System | Dynamic Conflict | Static Conflict | Conditional Conflict | Macro AA (Held-Out) | Micro AA (Held-Out) | All-30 Macro AA | Delta vs Perseus Vault |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Telarch R5 Profile (`harpe 0.1.0a8`)** | **0.848** | **0.739** | 0.943 | **0.843** | **0.848** | 0.851 | **+0.293** |
| **Telarch R5 Compact (`harpe 0.1.0a8`)** | 0.840 | **0.739** | 0.940 | **0.839** | 0.842 | **0.852** | **+0.289** |
| Telarch R4 Compact (`harpe 0.1.0a7`) | 0.764 | 0.701 | **0.946** | 0.804 | 0.779 | — | +0.254 |
| Perseus Vault (Historical Baseline) | 0.584 | 0.420 | 0.644 | 0.550 | 0.575 | 0.555 | Baseline |
| Honcho (`plastic-labs/honcho` v3.0.9) | 0.631 | 0.208 | 0.606 | 0.482 | 0.588 | 0.477 | −0.068 |
| Mem0 (`mem0ai[nlp]` 2.0.14) | 0.268 | 0.095 | 0.829 | 0.397 | 0.316 | 0.392 | −0.153 |
| Supermemory (Self-Hosted) | 0.121 | 0.091 | 0.670 | 0.294 | 0.181 | 0.288 | −0.256 |
| Hindsight (`hindsight-all` 0.8.6) | 0.442 | 0.110 | 0.267 | 0.273 | 0.390 | 0.281 | −0.277 |
| RetainDB Server (`@retaindb/server`) | 0.281 | 0.210 | 0.320 | 0.270 | 0.281 | 0.270 | −0.280 |
| Mnemosyne (v3.14.0) | 0.345 | −0.208 | 0.197 | 0.111 | 0.275 | 0.116 | −0.439 |

> **Methodological Disclosure & Dual Reporting Standard**: Personas 0–7 (1,006 questions) served as the initial development set for `ProfileLedger`. R4 Compact (`harpe 0.1.0a7`) establishes the strict blind out-of-sample baseline across held-out Personas 8–29 (2,744 questions), scoring 0.804 Macro AA / 0.779 Micro AA. R5 Profile (`harpe 0.1.0a8`) reflects post-diagnostic normalization where generalized birthdate parsing, employment status resolution, and chronological sorting refinements were implemented following diagnostic triage of Round 4 failure modes, achieving 0.843 Held-Out Macro AA and 0.852 All-30 Macro AA. Both arms demonstrate a +0.289 to +0.293 lead over Perseus Vault and outperform all evaluated external memory engines (+0.361 vs Honcho, +0.446 vs Mem0).


## What each program measures

The programs build the library source vendored in this repository (`harpe/`, `kibisis/`, `argus-ledger/`), not the crates.io releases. All data is synthetic, and everything runs in one process and in memory unless noted.

| Program | What it measures |
| --- | --- |
| `telarch-benchmarks` | Harpe selection time for 10 and 200 items under a token budget; Kibisis in-memory writes, each chained into the vault's audit journal; and a contended lease round-trip from 8 threads on 10 resources with the conflicts counted |
| `knapsack_cost_curve` | Harpe over 100,000 simulated turns: tokens kept compared with keeping everything, turns over the budget, and time per turn |
| `adversarial_contention` | The Kibisis in-process lease coordinator with 16 and 32 threads on one resource: successful acquire-release cycles per second and lease decisions per second (every answered `acquire`, rejections included) reported separately with the rejection rate, moments when two threads held the lease, fencing tokens that went backwards, and how long an abandoned lease takes to become acquirable again |
| `argus_1m_scale` | Argus with 1,000,000 events: in-memory append rate, a full verification walk, the time to locate an edit at event 500,000, and `--file-events N` (default 100,000) appends to the on-disk `FileLedger` with `sync_data` per event |
| `argus_anchored_checkpoints` | A rewritten chain caught by verifying against an anchored head. The program signs that head with Ed25519 in a C2SP signed-note style itself; Argus does not implement checkpoints |
| `swarm_partition` | 10 simulated nodes in one process under 0%, 25% and 50% simulated packet loss; after reconciliation each node's chain is verified and the per-node message sets are compared, so the report says whether the nodes converged and how many messages never arrived. At 25% and 50% the nodes do not converge because the loss model is permanent per (message, node) pair with one retry under the same rule — a harness property, closed-form (`reports/findings/B-A-swarm-partition-convergence.md`); no suite replication protocol is exercised |
| `restricted_payload_test` | The three libraries together under two small token budgets (60 and 150 tokens): tokens kept and payload size |

The Python scripts in `scripts/`:

- `gap1_harpe_quality.py` and `gap2_harpe_alternatives.py` compare Harpe with simple re-implementations of a sliding window and a recent-first trim. They do not run LangChain or LlamaIndex code. `gap2` times Harpe's Python package against them in the same process; the Rust crate's call times come from `telarch-benchmarks`.
- `gap3_kibisis_multiproc_crashes.py` runs 2 to 32 separate OS processes against one `SQLiteLeaseCoordinator` lease. It also checks that a sink comparing fencing tokens refuses the late write of a holder paused past its TTL (the pause is a sleep; no SIGSTOP signal is sent), and that the lease database passes `PRAGMA integrity_check` after a process holding the lease is killed with SIGKILL.
- `gap4_kibisis_model_check.py` is a breadth-first search over a lease and fencing-token state machine written to mirror `spec/kibisis_lease_fencing.tla`. It does not run the TLA+ model checker on that spec. It checks mutual exclusion and monotonic fencing in every reachable state within its bounds.
- `gap5_supply_chain.py` runs `cargo audit` and writes the CycloneDX SBOM in `sbom/`. It reports only those two results.
- `gap7_hygiene.py` records binary sizes and peak memory, and exports CSV files to `reports/raw_csvs/`.

## Reproduce a published figure

```bash
git clone --branch v0.1.0-alpha.10 https://github.com/Perseus-Computing-LLC/telarch-benchmarks
cd telarch-benchmarks
# then run the command shown under the figure on telarch.ai/evidence, for example:
cargo run --release -p telarch-benchmarks --bin argus_1m_scale
```

## Run everything

```bash
# Build the release binaries, run every program and script, write the reports to reports/ and regenerate claims.json:
make bench

# Tests, clippy and cargo-audit:
make check

# Vendored crate versions equal the ones claims.json names; README numbers equal claims.json:
make vendor-check claims-check
```

`make bench` runs each program and script once under `taskset -c 0-9` at nice 15 and the lowest best-effort I/O priority (the host is shared; two performance-core threads and the disk stay available to it), and `scripts/build_claims.py` reads every value in `claims.json` from that run's reports. For repeated runs, `scripts/run_once.sh <date> <run>` does the same and keeps a copy of the reports under `reports/runs/<date>/<run>/`; when such runs exist, `build_claims.py` publishes the median of each numeric claim across the latest date's runs, with its range and run count. Neither checks the load average: on a shared machine, start them only when `uptime` shows a 1-minute load average under 4. The committed `reports/` hold the last of the runs that `claims.json` summarises.

Or run one program:

```bash
cargo run --release -p telarch-benchmarks
cargo run --release -p telarch-benchmarks --bin knapsack_cost_curve
cargo run --release -p telarch-benchmarks --bin adversarial_contention
cargo run --release -p telarch-benchmarks --bin argus_1m_scale
cargo run --release -p telarch-benchmarks --bin argus_anchored_checkpoints
cargo run --release -p telarch-benchmarks --bin swarm_partition
cargo run --release -p telarch-benchmarks --bin restricted_payload_test
```

Each program also accepts `--json <path>` to write its report.

## Memory evaluations

[`evals/`](evals/README.md) runs four public long-term memory benchmarks against the libraries. Kibisis stores each conversation history, Harpe picks the context that fits the token budget, and Argus records every run.

- [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
- [LoCoMo](https://github.com/snap-research/locomo)
- [BEAM](https://github.com/mohammadtavakoli78/BEAM)
- [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench)

Each benchmark uses its official prompts and judges, loaded from pinned upstream commits.

```bash
make evals-setup && make evals-test
evals/.venv/bin/python -m telarch_evals fetch all
evals/.venv/bin/python -m telarch_evals longmemeval --per-type 1   # a 6-question check; drop --per-type for all 500
```

The published memory-quality figures from these evaluations are in [Standard Long-Term Memory Evaluations](#standard-long-term-memory-evaluations--competitive-baselines) above and on [telarch.ai/evidence](https://telarch.ai/evidence); the commands here reproduce them.

## Constraints

- The programs above make no network calls and send no telemetry. The memory evaluations in `evals/` download their datasets and call the model API you configure.
- Synthetic workloads; real agent workloads will differ.
- No independent security assessment of the libraries has been completed. Designed for local and disconnected use.

## License

Released under the [MIT License](LICENSE) © 2026 Perseus Computing LLC.
