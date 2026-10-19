# Telarch Benchmarks

**The programs behind every figure on [telarch.ai](https://telarch.ai).** Benchmarks, stress tests and fault injection for the three Telarch libraries: [Harpe](https://github.com/Perseus-Computing-LLC/harpe), [Kibisis](https://github.com/Perseus-Computing-LLC/kibisis) and [Argus](https://github.com/Perseus-Computing-LLC/argus).

[![CI](https://github.com/Perseus-Computing-LLC/telarch-benchmarks/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Perseus-Computing-LLC/telarch-benchmarks/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Part of Telarch by [Perseus Computing LLC](https://perseuscomputing.com).

## Headline results

Medians of five runs of these programs at release tag v0.1.0-alpha.4, with synthetic data on one shared server. Each figure on [telarch.ai/evidence](https://telarch.ai/evidence) is shown with every run, its range, the hardware, the raw output files and the command that reproduces it.

| Library | Result | Program |
| --- | --- | --- |
| Harpe | 78.3% fewer tokens sent over 100,000 simulated agent turns with a 1,200-token budget per turn; 0 turns over the budget | `knapsack_cost_curve` |
| Harpe | 1.72 µs median call for 10 items, 36.32 µs for 200 | `telarch-benchmarks` |
| Kibisis | 0 times two holders had the same lease in 64,000 contended attempts from 16 and 32 threads | `adversarial_contention` |
| Kibisis | 0 overlapping holders across 2 to 32 OS processes; a paused holder's late write rejected; the lease database usable after a holder is killed mid-write | `scripts/gap3_kibisis_multiproc_crashes.py` |
| Argus | 864,364 appends and 2,567,911 verified events per second, 1,000,000 events in memory | `argus_1m_scale` |
| Argus | An edit at event 500,000 found in 183.6 ms | `argus_1m_scale` |
| Argus | A fully re-hashed history caught by verifying against an anchored head | `argus_anchored_checkpoints` |

## What each program measures

The programs build the library source vendored in this repository (`harpe/`, `kibisis/`, `argus-ledger/`), not the crates.io releases. All data is synthetic, and everything runs in one process and in memory unless noted.

| Program | What it measures |
| --- | --- |
| `telarch-benchmarks` | Harpe selection time for 10 and 200 items under a token budget, and Kibisis in-memory writes, each chained into the vault's audit journal |
| `knapsack_cost_curve` | Harpe over 100,000 simulated turns: tokens kept compared with keeping everything, turns over the budget, and time per turn |
| `adversarial_contention` | The Kibisis in-process lease coordinator with 16 and 32 threads on one resource: throughput, moments when two threads held the lease, and fencing tokens that went backwards |
| `argus_1m_scale` | Argus with 1,000,000 events: append rate, a full verification walk, and the time to locate an edit at event 500,000 |
| `argus_anchored_checkpoints` | A rewritten chain caught by verifying against an anchored head. The program signs that head with Ed25519 in a C2SP signed-note style itself; Argus does not implement checkpoints |
| `swarm_partition` | 10 simulated nodes in one process under 0%, 25% and 50% simulated packet loss, with the audit chains checked after reconciliation |
| `restricted_payload_test` | The three libraries together under two small token budgets (60 and 150 tokens): tokens kept and payload size |

The Python scripts in `scripts/`:

- `gap1_harpe_quality.py` and `gap2_harpe_alternatives.py` compare Harpe with simple re-implementations of a sliding window and a recent-first trim. They do not run LangChain or LlamaIndex code.
- `gap3_kibisis_multiproc_crashes.py` runs separate OS processes against one `SQLiteLeaseCoordinator` lease, with SIGSTOP and SIGKILL fault injection.
- `gap4_kibisis_model_check.py` is a breadth-first search over a lease and fencing-token state machine written to mirror `spec/kibisis_lease_fencing.tla`. It does not run the TLA+ model checker on that spec.
- `gap5_supply_chain.py` writes the CycloneDX SBOM in `sbom/` and runs `cargo audit`.
- `gap7_hygiene.py` records binary sizes and peak memory, and exports CSV files to `reports/raw_csvs/`.

## Reproduce a published figure

```bash
git clone --branch v0.1.0-alpha.4 https://github.com/Perseus-Computing-LLC/telarch-benchmarks
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
```

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

## Constraints

- The programs make no network calls and send no telemetry.
- Synthetic workloads; real agent workloads will differ.
- No independent security assessment of the libraries has been completed. Designed for local and disconnected use.

## License

Released under the [MIT License](LICENSE) © 2026 Perseus Computing LLC.
