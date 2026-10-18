# Telarch Cognitive Infrastructure Benchmarks

> Deterministic microsecond latency, high-throughput lease concurrency, and network-partition tolerance benchmarks for the Telarch cognitive stack.

Part of the **Telarch Cognitive Infrastructure** (Telarch Suite / TCI) by [Perseus Computing LLC](https://perseuscomputing.com).

## Overview

This repository contains the benchmarking and stress-testing suite for the core Telarch co-processors:
- **Harpe**: Deterministic context synthesis & heuristic token budgeting.
- **Kibisis**: Encrypted cognitive memory vault with atomic lease concurrency.
- **Argus**: Tamper-evident append-only SHA-256 provenance ledger.

## Benchmark Results

### Microbenchmarks, Scale & Concurrency Stress

Measured on bare-metal Linux x86_64 (Intel Core i5-13500, runs pinned to the performance cores) with `rustc 1.97.1` and the default release profile (`opt-level = 3`, no LTO). Each report in [`reports/`](reports/) is the most representative of three runs (the run closest to the median of every figure it reports), and every measured figure below is copied from those reports.

* **Harpe (Context Budgeting & Knapsack Pruning)**:
  * Small context (10 items, 60-token budget): Throughput **~507,607 ops/s** | p50: **1.88 µs** | p95: **2.36 µs** | p99: **3.16 µs**
  * Large context (200 items, 1,500-token budget): Throughput **~20,680 ops/s** | p50: **41.16 µs** | Context Ceiling Reduction: **69.51%**
  * 100,000-turn production cost curve (`knapsack_cost_curve`): Average latency **46.90 µs/turn** (~21,323 turns/s) | Context reduction: **78.28%** | Budget overruns: **0 (0.00%)** | Net token savings: **407.6M tokens** ($1,222.80 saved per 100k turns at Claude Sonnet frontier rates [$3.00/1M, $0.30 cache]; $1,019.00 at Frontier Flagship rates; $40.76 at Gemini Flash rates)
* **Kibisis (In-Memory Ingestion & Concurrency Leases)**:
  * Sequential write throughput, each write hashed and chained into the audit journal: **271,222 writes/s**
  * Multi-agent lease concurrency under adversarial contention (`adversarial_contention`, 50 ms leases):
    * 16 workers: **1,466,471 ops/s** (0.68 µs latency) | 834 of 32,000 attempts granted, 31,166 blocked | Split-brain errors: **0** | Fencing regressions: **0**
    * 32 workers: **1,597,354 ops/s** (0.63 µs latency) | 473 of 32,000 attempts granted, 31,527 blocked | Split-brain errors: **0** | Fencing regressions: **0**
* **Argus (Tamper-Evident Ledger at 1M Scale)**:
  * 1,000,000 sequential appends through the public API, including payload validation, timestamping and SHA-256 chaining (`argus_1m_scale`): **809,698 events/s** (1.24 µs/op; 1.24s total)
  * Full 1,000,000-event chain verification (`verify_chain`): **2,433,053 events/s** (0.41 µs/event; 0.41s total)
  * Tamper detection: a payload altered at sequence #500,000 is detected and located in **185.35 ms**
* **Swarm Network Partition Tolerance**:
  * Evaluated across 10 edge nodes under 0%, 25%, and 50% simulated packet loss (`swarm_partition`)
  * State Convergence: **100%**
  * Split-Brain Inconsistencies: **0**
  * Gossip message throughput: **235,548–264,971 msgs/s** across the three scenarios
* **Restricted Environment Payload Gauntlet (Micro-Payload Tiers)**:
  * **Tier 0 (Tactical Pulse - 60 tokens)**: 800 raw tokens synthesized to 51 tokens (**93.62% context reduction**) | Wire payload: **132 bytes** JSON / **79 bytes** compact TLV binary | Harpe latency: **20.33 µs** | Argus append: **8.42 µs** (Chain integrity: 100% valid)
  * **Tier 1 (Minimal Closed-Loop - 150 tokens)**: 1,235 raw tokens synthesized to 139 tokens (**88.74% context reduction**) | Wire payload: **90 bytes** JSON | Harpe latency: **31.27 µs** | Kibisis fencing token: enforced (1) | Argus append: **3.73 µs** (Chain integrity: 100% valid)

## Running the Benchmarks

```bash
# Run core latency and throughput benchmarks
cargo run --release -p telarch-benchmarks

# Run multi-agent swarm partition tolerance simulation
cargo run --release -p telarch-benchmarks --bin swarm_partition

# Run restricted environment micro-payload test
cargo run --release -p telarch-benchmarks --bin restricted_payload_test
```

## Architecture & Licensing

All modules run locally in memory with zero external cloud dependencies or network telemetry.
Released under the MIT License © 2026 Perseus Computing LLC.
