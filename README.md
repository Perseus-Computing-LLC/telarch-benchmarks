# Perseus Cognitive Infrastructure Benchmarks

> Pure-Rust microsecond latency, high-throughput lease concurrency, LongMemEval-S recall, and network-partition tolerance benchmarks for the Perseus cognitive stack.

Part of the **Perseus Cognitive Infrastructure** suite by [Perseus Computing LLC](https://perseuscomputing.com).

## Overview

This repository contains the benchmarking and stress-testing suite for the core Perseus co-processors:
- **Harpe**: Deterministic context synthesis & heuristic token budgeting.
- **Kibisis**: Encrypted cognitive memory vault with atomic lease concurrency.
- **Argus**: Tamper-evident append-only SHA-256 provenance ledger.

## Benchmark Results

### Macro & Micro Benchmarks
* **Harpe (Context Budgeting)**:
  * p50 Latency: 1.72 µs
  * p95 Latency: 4.88 µs
  * Token Ceiling Reduction: 69.5%
* **Kibisis (Atomic Leases)**:
  * Operations / sec: 1,745,000+
  * Concurrency Contention: 0 race conditions detected
* **Argus (Audit Ledger)**:
  * Append Latency: 4.12 µs
  * Tamper Detection: 46 µs fail-closed verification
* **LongMemEval-S Recall**:
  * 500 instances across 23,854 sessions evaluated in 7.45 s
  * Recall@1: 87.20%
  * Recall@10: 98.20%
  * MRR: 0.9111
* **Swarm Network Partition Tolerance**:
  * Evaluated across 10 edge nodes under 0%, 25%, and 50% simulated packet loss
  * State Convergence: 100%
  * Split-Brain Errors: 0

## Running the Benchmarks

```bash
# Run core latency and throughput benchmarks
cargo run --release -p perseus-benchmarks

# Run multi-agent swarm partition tolerance simulation
cargo run --release -p perseus-benchmarks --bin swarm_partition
```

## Architecture & Licensing

All modules are pure Rust with zero external cloud dependencies or network telemetry.
Released under the permissive MIT License © 2026 Perseus Computing LLC.
