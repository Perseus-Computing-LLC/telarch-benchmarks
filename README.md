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

Measured on bare-metal Linux x86_64 (`rustc 1.85.0`, `opt-level = 3`, `lto = "fat"`):

* **Harpe (Context Budgeting & Knapsack Pruning)**:
  * Small context (10 items, 2,048 tokens): Throughput **~567,000 ops/s** | p50: **1.76 µs** | p95: **1.86 µs** | p99: **1.99 µs**
  * Large context (200 items, 40,960 tokens): Throughput **~27,900 ops/s** | p50: **35.74 µs** | Context Ceiling Reduction: **69.51%**
  * 100,000-turn production cost curve (`knapsack_cost_curve`): Average latency **24.3 µs/turn** (41,234 turns/s) | Context reduction: **84.77%** | Budget overruns: **0 (0.00%)** | Net token savings: **427.6M tokens** ($1,283.02 saved per 100k turns at Claude 3.5 Sonnet rates; $1,069.19 at GPT-4o rates)
* **Kibisis (In-Memory Ingestion & Concurrency Leases)**:
  * Sequential write throughput: **2,098,000+ writes/s**
  * Multi-agent lease concurrency under adversarial contention (`adversarial_contention`):
    * 16 workers: **1,696,000+ ops/s** (0.59 µs latency) | Split-brain errors: **0** | Fencing regressions: **0**
    * 32 workers: **1,776,000+ ops/s** (0.56 µs latency) | Split-brain errors: **0** | Fencing regressions: **0**
* **Argus (Tamper-Evident Ledger at 1M Scale)**:
  * 1,000,000 sequential event appends with continuous SHA-256 chaining (`argus_1m_scale`): **632,170 events/s** (1.58 µs/op; 1.58s total)
  * Full 1,000,000-event linear verification traversal: **770,758 events/s** (1.30 µs/event; 1.29s total)
  * Tamper detection: Single-bit mutation at sequence #500,000 detected and isolated in **640.48 ms**
* **Swarm Network Partition Tolerance**:
  * Evaluated across 10 edge nodes under 0%, 25%, and 50% simulated packet loss (`swarm_partition`)
  * State Convergence: **100%**
  * Split-Brain Inconsistencies: **0**
  * Gossip message throughput: **599,631 msgs/s**
* **Restricted Environment Payload Gauntlet (Micro-Payload Tiers)**:
  * **Tier 0 (Tactical Pulse - 60 tokens)**: 800 raw tokens synthesized to 51 tokens (**93.62% context reduction**) | Wire payload: **132 bytes** JSON / **79 bytes** compact TLV binary | Harpe latency: **21.25 µs** | Argus append: **4.80 µs** (Chain integrity: 100% valid)
  * **Tier 1 (Minimal Closed-Loop - 150 tokens)**: 1,235 raw tokens synthesized to 139 tokens (**88.74% context reduction**) | Wire payload: **90 bytes** JSON | Harpe latency: **40.43 µs** | Kibisis fencing token: enforced (1) | Argus append: **3.71 µs** (Chain integrity: 100% valid)

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
