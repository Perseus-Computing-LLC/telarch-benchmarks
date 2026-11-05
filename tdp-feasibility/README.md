# TDP-CryptEng Feasibility Micro-Benchmarks & Validation Suite

This directory contains the empirical micro-benchmark probe and reproducible validation datasets for **TDP-CryptEng** (Department of the Air Force SBIR Topic `DAF27BZ01-DV008`: *Technical Data Package Modernization for Information Assurance and Secure Data Exchange*).

## Overview

The feasibility probe (`tdp_feasibility_probe.py`) evaluates three foundational architectural pillars required for high-assurance, air-gapped Model-Based Definition (MBD) and Technical Data Package (TDP) modernization:
1. **Linear Cryptographic Hash Chaining & Tamper Detection:** Canonical serialization and linear SHA-256 hash chaining using Argus ledger canonical encoding (`argus_ledger.canonical`), detecting unauthorized sub-millimeter coordinate mutations.
2. **Deterministic Structural Compliance & PMI Screening:** Automated inspection of ISO 10303 STEP structures against MIL-STD-31000B product context requirements and ASME Y14.41 geometric uncertainty assertions.
3. **Single-Writer Multi-Reader (SWMR) Concurrency & Mutual Exclusion:** Ruggedized tablet lease coordination using Kibisis (`kibisis.FileLeaseCoordinator`) to guarantee flightline data integrity and prevent split-brain writes during communications blackout.

## Test Fixture Manifest

| Fixture Name | Standard / Schema | Entity Count | Size | Description & Origin |
| :--- | :--- | :--- | :--- | :--- |
| `Ctl011.stp` | ISO 10303-214 | 55 | 2.6 KB | NIST MBE PMI Control Model (validates `DIMENSIONAL_SIZE` semantic PMI) |
| `pushrod_ap242.stp` | ISO 10303-242 (`10303 442`) | 80 | 4.3 KB | Accelerator Pushrod component model (METRE units, 50 µm shift evaluated) |
| `y_idler_ap242.stp` | ISO 10303-242 (`10303 442`) | 3,394 | 143.8 KB | Structural airframe idler bracket (METRE units, 50 µm shift evaluated) |
| `Pf033.stp` | ISO 10303-214 | 4,485 | 155.6 KB | NIST MBE Closed Shell assembly fixture |
| `cowl_ap242.stp` | ISO 10303-242 (`10303 442`) | 9,230 | 478.8 KB | Complex airframe cowl assembly (METRE units, 50 µm shift evaluated) |
| `synthetic_assembly_100k.stp` | ISO 10303-242 (`10303 442`) | 99,999 | 4.15 MB | Scaled multi-component airframe assembly (100k entities) |

## Reproduction Instructions

To execute the N=30 trial micro-benchmark suite on your local workstation:

```bash
# 1. Install prerequisites (Argus ledger and Kibisis vault)
pip install argus-ledger kibisis

# 2. Run the feasibility probe
python3 tdp_feasibility_probe.py
```

The probe outputs full median and 95th-percentile (p95) execution latencies, sustained ingestion/hash throughput (MB/s), tamper detection verification, and concurrency lease mutual exclusion state to `feasibility_probe_results.json`.
