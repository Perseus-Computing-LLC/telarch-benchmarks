# Telarch Benchmarks

Benchmark and stress-test programs for the three Telarch libraries: Harpe, Kibisis and Argus.

Part of the **Telarch Suite** by [Perseus Computing LLC](https://perseuscomputing.com).
The software is licensed under the [MIT License](LICENSE).

## Results

Measured results are on [telarch.ai/evidence](https://telarch.ai/evidence). Each figure there is the median of five runs of these programs at a release tag, shown with the range of the runs, the command that reproduces it and the raw output files.

This README no longer lists figures. The ones it used to show came from `claims.json`, and those values are typed into `scripts/build_claims.py` from single runs at commit `ca3dff7` rather than read from the reports in `reports/`, so several of them do not match those reports.

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

## Running the benchmarks

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
- Designed for local and disconnected use. No independent security assessment of the libraries has been completed.
- Released under the MIT License © 2026 Perseus Computing LLC.
