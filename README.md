# Telarch Cognitive Infrastructure Benchmarks

> Deterministic microsecond latency, high-throughput lease concurrency, and network-partition tolerance benchmarks for the Telarch cognitive stack.

Part of the **Telarch Cognitive Infrastructure** (Telarch Suite / TCI) by [Perseus Computing LLC](https://perseuscomputing.com).

## Overview

This repository contains the benchmarking and stress-testing suite for the core Telarch co-processors:
- **Harpe**: Deterministic context synthesis & heuristic token budgeting.
- **Kibisis**: Encrypted cognitive memory vault with atomic lease concurrency.
- **Argus**: Tamper-evident append-only SHA-256 provenance ledger.

## Benchmark Results

### Microbenchmarks & Concurrency Stress

Measured on bare-metal Linux x86_64 (`rustc 1.85.0`, `opt-level = 3`, `lto = "fat"`):

* **Harpe (Context Budgeting & Knapsack Pruning)**:
  * Small context (10 items, 2,048 tokens): Throughput **~640,000 ops/s** | p50: **1.51 µs** | p95: **1.61 µs** | p99: **1.69 µs**
  * Large context (200 items, 40,960 tokens): Throughput **~26,000 ops/s** | p50: **31.89 µs** | Context Ceiling Reduction: **69.51%**
* **Kibisis (In-Memory Ingestion & Concurrency Leases)**:
  * Sequential write throughput: **2,100,000+ writes/s**
  * Multi-agent lease concurrency (8 contending threads): **1,560,000+ ops/s**
  * Contention race conditions: **0 errors**
* **Argus (Tamper-Evident Ledger)**:
  * Sequential event append with SHA-256 chaining: **~600,000 events/s** (~1.67 µs/op)
  * Full-chain verification throughput: **~839,000 events/s** (~1.19 µs/event across 10,000-event sequence)
  * Tamper detection latency: **46.50 µs** (immediate fail-closed detection at corrupted block #42)
  * Tamper detection rate: **100.00%** (single-bit flip sensitivity)
* **Swarm Network Partition Tolerance**:
  * Evaluated across 10 edge nodes under 0%, 25%, and 50% simulated packet loss
  * State Convergence: **100%**
  * Split-Brain Inconsistencies: **0**
  * Post-partition reconciliation: **0.42 ms**
* **Restricted Environment Payload Gauntlet (Micro-Payload Tiers)**:
  * **Tier 0 (Tactical Pulse - 60 tokens)**: 800 raw tokens synthesized to 51 tokens (**93.62% context reduction**) | Wire payload: **132 bytes** JSON / **79 bytes** compact TLV binary | Knapsack latency: **150.63 µs** | Argus append: **21.94 µs** (Chain integrity: 100% valid)
  * **Tier 1 (Minimal Closed-Loop - 150 tokens)**: 1,235 raw tokens synthesized to 139 tokens (**88.74% context reduction**) | Wire payload: **90 bytes** JSON | Knapsack latency: **237.74 µs** | Kibisis fencing token: enforced (1) | Argus append: **12.60 µs** (Chain integrity: 100% valid)

### External Evaluations & Claims Retirement Note

In accordance with Telarch claims governance and the Agent Memory Atlas standards:
- **LongMemEval**: Offline benchmark evaluation suite is currently being ported to the modular scaffold. Historical monolithic runs demonstrated 73.8% (vanilla) and 79.0% (official-cot) accuracy. All uncommitted external claims (e.g. 0.9111 MRR) have been formally retired until the standalone multi-seed evaluation harness and JSON run artifacts are committed to this repository. See `CLAIMS-AUDIT.md`.

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
