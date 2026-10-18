# Telarch Benchmarks

> Measured performance, scale, and empirical verification benchmarks for the Telarch cognitive co-processors (Harpe, Kibisis, Argus).

Part of the **Telarch Suite** by [Perseus Computing LLC](https://perseuscomputing.com).
The software is licensed under the [MIT License](LICENSE).

## Overview

This repository contains the empirical benchmarking, stress-testing, and formal verification suite for Telarch:
- **Harpe**: Deterministic context synthesis and token budgeting under hard token ceilings.
- **Kibisis**: Encrypted cognitive memory vault with atomic Single-Writer Multi-Reader (SWMR) concurrency leases and monotonic fencing tokens.
- **Argus**: Tamper-evident append-only SHA-256 audit and provenance ledger.

All figures below are generated directly from machine-readable JSON reports in [`reports/`](reports/) and validated by [`claims.json`](claims.json) against [`claims_schema.json`](claims_schema.json).

## Benchmark Results

### Measured on Linux x86_64 (Intel Core i5-13500, 20 threads, performance cores pinned) with `rustc 1.97.1` (`--release`).

* **Harpe (Context Budgeting & Priority Selection)**:
  * Small context (10 items, 60-token ceiling): Throughput **~507,607 ops/s** | p50: **1.88 µs** (range 1.69–2.30 µs) | p95: **2.36 µs** | p99: **3.16 µs**
  * Large context (200 items, 1,500-token ceiling): Throughput **~20,680 ops/s** | p50: **41.16 µs** (range 37.68–43.51 µs) | Context Ceiling Reduction: **69.51%**
  * 100,000-turn production cost curve (`knapsack_cost_curve`): Average latency **46.90 µs/turn** (~21,323 turns/s) | Context reduction: **78.28%** | Budget overruns: **0 (0.00%)** | Net token reduction: **407.6M tokens** over 100,000 turns vs unbudgeted accumulation.
  * Quality under budget (Gap 1): **100.0% key fact recall** (95% CI: [100.0%, 100.0%]) with 57.97% token reduction vs unbudgeted baseline (compared to 0.22% for sliding window and 33.56% for LangChain trim_messages).
  * Latency comparison vs alternatives (Gap 2): Harpe selection represents **0.00038%** of a typical 500 ms LLM turn latency.

* **Kibisis (Memory Ingestion & Concurrency Leases)**:
  * Sequential write throughput, each write hashed and chained into the vault audit journal: **271,222 writes/s**
  * Multi-agent lease concurrency under adversarial contention (`adversarial_contention`, 50 ms requested lease TTL):
    * 16 workers: **1,466,471 ops/s** (0.68 µs latency) | Split-brain errors: **0** | Fencing rollbacks: **0**
    * 32 workers: **1,597,354 ops/s** (0.63 µs latency) | Split-brain errors: **0** | Fencing rollbacks: **0**
  * Multi-process crash resilience (Gap 3): 2 to 32 separate OS child processes competing via `SQLiteLeaseCoordinator`: **0 split-brain**, **0 stale writes accepted** by fencing-aware sink after pause past TTL, and 100% automatic SQLite WAL recovery after `SIGKILL (kill -9)`.
  * Formal model checking (Gap 4): TLA+ state-space exploration of lease state machine verified across 6,869 reachable states and 30,040 transitions: **100% valid, 0 invariant violations**.

* **Argus (Tamper-Evident Ledger at 1M Scale)**:
  * 1,000,000 sequential appends through public API with SHA-256 chaining (`argus_1m_scale`): **809,698 events/s** (1.24 µs/op; 1.24s total)
  * Full 1,000,000-event linear chain verification (`verify_chain`): **2,433,053 events/s** (0.41 µs/event; 0.41s total)
  * Tamper detection: a payload altered at sequence #500,000 is detected and located in **185.35 ms**
  * Anchored heads and signed checkpoints (Gap 6): Ed25519 signed checkpoints in C2SP signed-note format (signed in 35.5 µs). Proves that an attacker who alters event 5,000 and rewrites all subsequent hashes is **100% caught and rejected** when verified against an anchored checkpoint.

* **Swarm Network Partition Tolerance**:
  * Evaluated across 10 nodes under 0%, 25%, and 50% simulated packet loss (`swarm_partition`): **100% state convergence**, **0 split-brain inconsistencies**.

* **Restricted Environment Payload Gauntlet**:
  * **Tier 0 (Tactical Pulse - 60 tokens)**: 800 raw tokens synthesized to 51 tokens (**93.62% context reduction**) | Wire payload: **132 bytes** JSON / **79 bytes** compact TLV binary | Harpe latency: **20.33 µs** | Argus append: **8.42 µs**.
  * **Tier 1 (Minimal Closed-Loop - 150 tokens)**: 1,235 raw tokens synthesized to 139 tokens (**88.74% context reduction**) | Wire payload: **90 bytes** JSON | Harpe latency: **31.27 µs** | Kibisis fencing token: enforced (1) | Argus append: **3.73 µs**.

## Reproducing the Benchmarks

All benchmarks reproduce from a single command:

```bash
# Build release binaries, execute full benchmark suite, and regenerate claims.json:
make bench

# Run tests, clippy, and cargo-audit:
make check
```

Or run individual harnesses:

```bash
# Core latency and throughput
cargo run --release -p telarch-benchmarks

# 100,000-turn cost curve
cargo run --release -p telarch-benchmarks --bin knapsack_cost_curve

# Adversarial contention
cargo run --release -p telarch-benchmarks --bin adversarial_contention

# 1,000,000 event scale test
cargo run --release -p telarch-benchmarks --bin argus_1m_scale

# Argus anchored checkpoints (Gap 6)
cargo run --release -p telarch-benchmarks --bin argus_anchored_checkpoints

# Tactical swarm partition simulation
cargo run --release -p telarch-benchmarks --bin swarm_partition

# Restricted environment micro-payload test
cargo run --release -p telarch-benchmarks --bin restricted_payload_test
```

## Architecture & Constraints

- All modules run locally in memory without external network calls or telemetry.
- Designed for local and disconnected use. No independent security assessment of the libraries has been completed.
- Released under the MIT License © 2026 Perseus Computing LLC.
