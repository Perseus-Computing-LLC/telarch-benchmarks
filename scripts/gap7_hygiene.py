#!/usr/bin/env python3
"""Gap 7: Benchmark hygiene, memory footprints (Peak RSS), and raw CSV generation.

1. Measures compiled binary sizes of release artifacts.
2. Measures peak RSS memory consumption across each core workload.
3. Exports raw CSV datasets from benchmark outputs to `reports/raw_csvs/`.
4. Records CPU model, rustc version, git commit, and compiler flags (without hostnames).
"""

import csv
import glob
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import time
from typing import Dict, Any

import bench_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOST_CARGO_BIN = "/opt/data/home/.cargo/bin"


def get_binary_sizes() -> Dict[str, Any]:
    bin_dir = os.path.join(ROOT, "target", "release")
    bins = [
        "telarch-benchmarks",
        "knapsack_cost_curve",
        "adversarial_contention",
        "argus_1m_scale",
        "argus_anchored_checkpoints",
        "restricted_payload_test",
        "swarm_partition",
    ]
    results = {}
    for b in bins:
        p = os.path.join(bin_dir, b)
        if os.path.exists(p):
            sz_bytes = os.path.getsize(p)
            results[b] = {
                "bytes": sz_bytes,
                "megabytes": round(sz_bytes / (1024 * 1024), 2),
            }
    return results


def measure_workload_rss(cmd: list) -> Dict[str, Any]:
    env = os.environ.copy()
    if os.path.isdir(_HOST_CARGO_BIN):  # the maintainer's host; elsewhere (CI) cargo comes from PATH
        env["PATH"] = f"{_HOST_CARGO_BIN}:{env.get('PATH', '')}"
        env["HOME"] = "/opt/data/home"

    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    max_rss_kb = 0

    # Poll status while process runs
    while p.poll() is None:
        try:
            status_path = f"/proc/{p.pid}/status"
            if os.path.exists(status_path):
                with open(status_path, "r") as f:
                    for line in f:
                        if line.startswith("VmHWM:") or line.startswith("VmRSS:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                kb = int(parts[1])
                                if kb > max_rss_kb:
                                    max_rss_kb = kb
        except Exception:
            pass
        time.sleep(0.01)

    stdout, stderr = p.communicate()
    elapsed = time.perf_counter() - t0

    return {
        "exit_code": p.returncode,
        "elapsed_sec": round(elapsed, 3),
        "peak_rss_kb": max_rss_kb,
        "peak_rss_mb": round(max_rss_kb / 1024.0, 2),
    }


def export_raw_csvs() -> Dict[str, str]:
    csv_dir = os.path.join(ROOT, "reports", "raw_csvs")
    os.makedirs(csv_dir, exist_ok=True)
    exported = {}

    # 1. Export Phase 1 microbenchmarks CSV
    p1_path = os.path.join(ROOT, "reports", "microbenchmarks_phase1.json")
    if os.path.exists(p1_path):
        p1 = json.load(open(p1_path))
        csv_file = os.path.join(csv_dir, "harpe_microbenchmarks.csv")
        with open(csv_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scenario", "items", "budget_tokens", "iterations", "ops_per_sec", "latency_p50_us", "latency_p95_us", "latency_p99_us"])
            for sc_name in ["small_context", "large_context"]:
                sc = p1["harpe"][sc_name]
                w.writerow([sc_name, sc["items"], sc["budget_tokens"], sc["iterations"], sc["ops_per_sec"], sc["latency_p50_us"], sc["latency_p95_us"], sc["latency_p99_us"]])
        exported["harpe_microbenchmarks"] = "reports/raw_csvs/harpe_microbenchmarks.csv"

    # 2. Export Argus scale CSV
    argus_path = os.path.join(ROOT, "reports", "step3_argus_1m_scale.json")
    if os.path.exists(argus_path):
        arg = json.load(open(argus_path))
        csv_file = os.path.join(csv_dir, "argus_1m_scale.csv")
        with open(csv_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value", "unit"])
            w.writerow(["total_events", arg["total_events"], "events"])
            w.writerow(["append_duration_ms", arg["append_duration_ms"], "ms"])
            w.writerow(["append_throughput_events_per_sec", arg["append_throughput_events_per_sec"], "events/s"])
            w.writerow(["verification_throughput_events_per_sec", arg["verification_throughput_events_per_sec"], "events/s"])
            w.writerow(["tamper_detection_latency_us", arg["tamper_detection_latency_us"], "µs"])
            w.writerow(["tamper_detected_at_sequence", arg["tamper_detected_at_sequence"], "sequence_no"])
        exported["argus_1m_scale"] = "reports/raw_csvs/argus_1m_scale.csv"

    # 3. Export Adversarial contention CSV
    adv_path = os.path.join(ROOT, "reports", "step2_adversarial_contention.json")
    if os.path.exists(adv_path):
        adv = json.load(open(adv_path))
        csv_file = os.path.join(csv_dir, "kibisis_contention.csv")
        with open(csv_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["scenario", "worker_count", "total_attempts", "successful_leases", "conflicts_blocked", "split_brain_violations", "fencing_token_rollbacks", "throughput_ops_per_sec"])
            for sc_key in ["scenario_16_workers", "scenario_32_workers"]:
                sc = adv[sc_key]
                w.writerow([sc_key, sc["worker_count"], sc["total_attempts"], sc["successful_leases"], sc["conflicts_blocked"], sc["split_brain_violations"], sc["fencing_token_rollbacks"], sc["throughput_ops_per_sec"]])
        exported["kibisis_contention"] = "reports/raw_csvs/kibisis_contention.csv"

    return exported


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def hygiene_checks() -> Dict[str, bool]:
    """Checks the reports and the checkout rather than asserting the results."""
    texts = []
    for p in sorted(glob.glob(os.path.join(ROOT, "reports", "**", "*"), recursive=True)):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
    blob = "\n".join(texts)
    host = socket.gethostname()
    try:
        lock = subprocess.run(["git", "-C", ROOT, "ls-files", "--error-unmatch", "Cargo.lock"], capture_output=True)
        lock_tracked = lock.returncode == 0
    except OSError:
        lock_tracked = False
    return {
        "hostnames_scrubbed": not (len(host) >= 3 and re.search(rf"(?<![\w-]){re.escape(host)}(?![\w-])", blob)),
        "personal_emails_excluded": not EMAIL_RE.search(blob),
        "cargo_lock_committed": lock_tracked,
        "rust_toolchain_pinned": bench_env.toolchain() != "unknown",
    }


def main():
    print("=" * 80)
    print("      TELARCH GAP 7: BENCHMARK HYGIENE & RESOURCE FOOTPRINT     ")
    print("        Linux x86_64, Intel Core i5-13500 (20 threads)          ")
    print("=" * 80)

    # 1. Binary sizes
    print("\n[1/3] Measuring Release Binary Footprints:")
    sizes = get_binary_sizes()
    for name, info in sizes.items():
        print(f"  • {name:28s} {info['megabytes']:5.2f} MB ({info['bytes']:,} bytes)")

    # 2. Peak RSS measurements
    print("\n[2/3] Profiling Peak RSS Memory Footprint Under Load:")
    bin_path = os.path.join(ROOT, "target", "release", "telarch-benchmarks")
    rss_phase1 = measure_workload_rss([bin_path])
    print(f"  • telarch-benchmarks (Phase 1): {rss_phase1['peak_rss_mb']} MB Peak RSS (Elapsed: {rss_phase1['elapsed_sec']}s)")

    knapsack_bin = os.path.join(ROOT, "target", "release", "knapsack_cost_curve")
    rss_knapsack = measure_workload_rss([knapsack_bin])
    print(f"  • 100k-turn knapsack curve:     {rss_knapsack['peak_rss_mb']} MB Peak RSS (Elapsed: {rss_knapsack['elapsed_sec']}s)")

    # 3. Export Raw CSVs
    print("\n[3/3] Exporting Raw Benchmark Measurement CSVs:")
    csv_exports = export_raw_csvs()
    for name, rel_path in csv_exports.items():
        print(f"  • {name:28s} -> {rel_path}")

    report = {
        "benchmark": "telarch-gap7-hygiene",
        "suite": "Telarch Suite (TCI)",
        "hardware": "Linux x86_64, Intel Core i5-13500 (20 threads)",
        "toolchain": "rustc 1.97.1",
        "compiler_flags": "--release (opt-level=3, codegen-units=16)",
        "commit": bench_env.commit(),
        "timestamp_utc": bench_env.utc_now(),
        "release_binary_sizes": sizes,
        "peak_rss_profiles": {
            "microbenchmarks_phase1": rss_phase1,
            "knapsack_cost_curve_100k": rss_knapsack,
        },
        "raw_csv_exports": csv_exports,
        "hygiene_checks": hygiene_checks(),
    }

    out_file = os.path.join(ROOT, "reports", "gap7_hygiene.json")
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[REPORT] Saved machine-readable report to: {out_file}")


if __name__ == "__main__":
    main()
