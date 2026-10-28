use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use argus_ledger::{
    AuditLedgerReader, AuditLedgerWriter, AuditVerifier, InMemoryLedger, VerificationPolicy,
};
use harpe::{ContextBudgetEngine, ContextItem, ContextSynthesizer, PrunePolicy, TokenBudget};
use kibisis::{
    InMemoryLeaseCoordinator, InMemoryMemoryVault, LeaseCoordinator, MemoryEntity, MemoryVault,
};
use serde::Serialize;

mod clock;
mod host;

#[derive(Serialize)]
struct BenchmarkReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
    timestamp_utc: String,
    harpe: HarpeReport,
    kibisis: KibisisReport,
    argus: ArgusReport,
}

#[derive(Serialize)]
struct HarpeReport {
    small_context: ScenarioReport,
    large_context: ScenarioReport,
    token_optimization_pct: f64,
}

#[derive(Serialize)]
struct ScenarioReport {
    items: usize,
    input_tokens: usize,
    budget_tokens: usize,
    iterations: usize,
    ops_per_sec: f64,
    latency_p50_us: f64,
    latency_p95_us: f64,
    latency_p99_us: f64,
}

#[derive(Serialize)]
struct KibisisReport {
    vault_put_entities: usize,
    vault_put_duration_ms: f64,
    vault_put_ops_per_sec: f64,
    vault_search_iterations: usize,
    vault_search_p50_us: f64,
    vault_search_p95_us: f64,
    vault_search_p99_us: f64,
    vault_audit_verify_events: usize,
    vault_audit_verify_duration_ms: f64,
    vault_audit_verify_ops_per_sec: f64,
    concurrency_leases: LeaseReport,
}

/// Contended lease round-trip: acquire, renew, release with the coordinator lock dropped between
/// calls, so other threads can be refused while a lease is held.
#[derive(Serialize)]
struct LeaseReport {
    label: &'static str,
    threads: usize,
    resources: usize,
    iters_per_thread: usize,
    total_attempts: usize,
    duration_ms: f64,
    /// Completed acquire->renew->release round trips per second.
    round_trips_per_sec: f64,
    /// Every acquire call per second, rejections included.
    attempts_per_sec: f64,
    acquisitions: usize,
    conflict_rejections: usize,
}

#[derive(Serialize)]
struct ArgusReport {
    append_events: usize,
    append_duration_ms: f64,
    append_ops_per_sec: f64,
    verify_events: usize,
    verify_duration_ms: f64,
    verify_ops_per_sec: f64,
    tamper_detection_us: f64,
    tamper_edited_event: u64,
    tamper_reported_event: u64,
    tamper_resistance_pass: bool,
}

fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() as f64) * (pct / 100.0)).round() as usize;
    let clamped = idx.min(sorted.len() - 1);
    sorted[clamped]
}

/// 4900 -> "4,900"
fn grouped(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// One acquire -> renew -> release round trip. Each call locks the coordinator on its own, so the
/// lease is visibly held by this thread between the calls and other threads can be refused.
/// Returns false when the acquire was rejected because another holder had the resource.
fn lease_round_trip(
    coord: &std::sync::Mutex<InMemoryLeaseCoordinator>,
    resource: &str,
    holder: &str,
) -> bool {
    let acquired = coord
        .lock()
        .unwrap()
        .acquire(resource, holder, Duration::from_secs(5));
    let lease = match acquired {
        Ok(lease) => lease,
        Err(_) => return false,
    };
    // Nothing is held here, so the scheduler can interleave another thread's acquire.
    std::hint::spin_loop();
    let _ = coord.lock().unwrap().renew(&lease, Duration::from_secs(10));
    let _ = coord.lock().unwrap().release(&lease);
    true
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================================");
    println!("              TELARCH COGNITIVE INFRASTRUCTURE: PHASE 1 BENCHMARKS            ");
    println!("          Telarch Suite (TCI) by Perseus Computing LLC                        ");
    println!("================================================================================\n");

    // ---------------------------------------------------------
    // 1. HARPE BENCHMARKS (CONTEXT SYNTHESIS & BUDGETING)
    // ---------------------------------------------------------
    println!("[1/3] Benchmarking HARPE Context Engine (Deterministic Token Budgeting)...");
    let engine = ContextBudgetEngine::default();

    // Small scenario: 10 items, the first two pinned, under a 60-token budget
    let small_budget_tokens = 60;
    let small_items: Vec<ContextItem> = (0..10)
        .map(|i| {
            let priority = if i < 2 { 0 } else { (i * 10) as u8 };
            ContextItem::new(
                format!("item_{}", i),
                format!("Tactical feed sensor entry {} with operational coordinates and mission parameters.", i),
                priority,
            )
        })
        .collect();
    let small_budget = TokenBudget::new(small_budget_tokens);

    let mut small_latencies = Vec::new();
    let small_iters = 2000;
    let t0 = Instant::now();
    for _ in 0..small_iters {
        let it = Instant::now();
        let _ = engine
            .synthesize(&small_items, &small_budget, PrunePolicy::LowestPriority)
            .unwrap();
        small_latencies.push(it.elapsed().as_nanos() as f64 / 1000.0);
    }
    let small_total_time = t0.elapsed();
    small_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    // Large scenario: 200 items, two at each priority from 0 (pinned) to 99, under a 1,500-token budget
    let large_budget_tokens = 1500;
    let large_items: Vec<ContextItem> = (0..200)
        .map(|i| {
            let priority = (i % 100) as u8;
            ContextItem::new(
                format!("repo_file_{}", i),
                format!("fn process_subsystem_{}() -> Result<(), Error> {{ let x = 42; println!(\"running cycle\"); Ok(()) }}", i),
                priority,
            )
        })
        .collect();
    let large_budget = TokenBudget::new(large_budget_tokens);

    let mut large_latencies = Vec::new();
    let large_iters = 500;
    let t1 = Instant::now();
    let mut last_report = None;
    for _ in 0..large_iters {
        let it = Instant::now();
        let report = engine
            .synthesize(&large_items, &large_budget, PrunePolicy::LowestPriority)
            .unwrap();
        large_latencies.push(it.elapsed().as_nanos() as f64 / 1000.0);
        last_report = Some(report);
    }
    let large_total_time = t1.elapsed();
    large_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let rep = last_report.unwrap();
    let large_tokens = rep.consumed_tokens + rep.pruned_tokens;
    let reduction_pct = (rep.pruned_tokens as f64 / large_tokens as f64) * 100.0;
    // One more call, after both timed loops so the timings are measured as before, counts the small items' tokens
    let small_rep = engine
        .synthesize(&small_items, &small_budget, PrunePolicy::LowestPriority)
        .unwrap();
    let small_tokens = small_rep.consumed_tokens + small_rep.pruned_tokens;

    let small_p50 = percentile(&small_latencies, 50.0);
    let small_p95 = percentile(&small_latencies, 95.0);
    let small_p99 = percentile(&small_latencies, 99.0);
    let small_ops = small_iters as f64 / small_total_time.as_secs_f64();

    let large_p50 = percentile(&large_latencies, 50.0);
    let large_p95 = percentile(&large_latencies, 95.0);
    let large_p99 = percentile(&large_latencies, 99.0);
    let large_ops = large_iters as f64 / large_total_time.as_secs_f64();

    println!(
        "  -> Small ({} items, {} tokens, budget {}):",
        small_items.len(),
        grouped(small_tokens),
        grouped(small_budget_tokens)
    );
    println!("     Throughput: {:.0} ops/sec", small_ops);
    println!(
        "     Latency: p50={:.2} µs | p95={:.2} µs | p99={:.2} µs",
        small_p50, small_p95, small_p99
    );
    println!(
        "  -> Large ({} items, {} tokens, budget {}):",
        large_items.len(),
        grouped(large_tokens),
        grouped(large_budget_tokens)
    );
    println!("     Throughput: {:.0} ops/sec", large_ops);
    println!(
        "     Latency: p50={:.2} µs | p95={:.2} µs | p99={:.2} µs",
        large_p50, large_p95, large_p99
    );
    println!(
        "     Token Optimization: {:.2}% context pruned to enforce budget bound\n",
        reduction_pct
    );

    // ---------------------------------------------------------
    // 2. KIBISIS BENCHMARKS (VAULT & CONCURRENCY LEASES)
    // ---------------------------------------------------------
    println!("[2/3] Benchmarking KIBISIS Memory Vault & Concurrency Leases...");
    let mut vault = InMemoryMemoryVault::new();

    // Ingestion benchmark: 10,000 entities
    let n_entities = 10000;
    let t_vault_start = Instant::now();
    for i in 0..n_entities {
        let ent = MemoryEntity::new(
            "tactical_state",
            format!("unit_telemetry_{}", i),
            format!(
                "lat:30.2672, lon:-97.7431, battery:98%, status:active, seq:{}",
                i
            ),
        );
        vault.put(&ent).unwrap();
    }
    let vault_put_dur = t_vault_start.elapsed();
    let vault_put_throughput = n_entities as f64 / vault_put_dur.as_secs_f64();

    // Query / search benchmark
    let mut search_latencies = Vec::new();
    let search_iters = 1000;
    for i in 0..search_iters {
        let q = format!("unit_telemetry_{}", (i * 7) % n_entities);
        let it = Instant::now();
        let res = vault.search(&q, Some("tactical_state"), 1).unwrap();
        assert!(!res.is_empty());
        search_latencies.push(it.elapsed().as_nanos() as f64 / 1000.0);
    }
    search_latencies.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let search_p50 = percentile(&search_latencies, 50.0);
    let search_p95 = percentile(&search_latencies, 95.0);
    let search_p99 = percentile(&search_latencies, 99.0);

    // Contended lease round-trip: 8 threads, 10 shared resources. Each coordinator call takes the
    // Mutex on its own, so another thread can see the lease held between acquire and release.
    // Holding one guard across acquire/renew/release serialised the threads and made conflicts
    // impossible, which is why the old figure never showed one.
    let coordinator = Arc::new(std::sync::Mutex::new(InMemoryLeaseCoordinator::new()));
    let num_threads = 8;
    let iters_per_thread = 2000;
    let successful_leases = Arc::new(AtomicUsize::new(0));
    let conflict_rejections = Arc::new(AtomicUsize::new(0));

    let t_lease_start = Instant::now();
    let mut handles = Vec::new();
    for tid in 0..num_threads {
        let coord = Arc::clone(&coordinator);
        let succ = Arc::clone(&successful_leases);
        let conf = Arc::clone(&conflict_rejections);
        handles.push(thread::spawn(move || {
            let holder = format!("agent_{}", tid);
            for i in 0..iters_per_thread {
                let resource = format!("resource_{}", i % 10);
                if lease_round_trip(&coord, &resource, &holder) {
                    succ.fetch_add(1, Ordering::Relaxed);
                } else {
                    conf.fetch_add(1, Ordering::Relaxed);
                }
            }
        }));
    }

    for h in handles {
        h.join().unwrap();
    }
    let lease_total_dur = t_lease_start.elapsed();
    let total_lease_attempts = num_threads * iters_per_thread;
    let total_acquisitions = successful_leases.load(Ordering::Relaxed);
    let total_conflicts = conflict_rejections.load(Ordering::Relaxed);
    // Completed round trips per second; a rejected acquire is not a round trip.
    let lease_throughput = total_acquisitions as f64 / lease_total_dur.as_secs_f64();
    let lease_attempts_per_sec = total_lease_attempts as f64 / lease_total_dur.as_secs_f64();

    println!(
        "  -> Memory Vault Put Ingestion: {:.0} writes/sec ({:.2} ms total for 10k entities)",
        vault_put_throughput,
        vault_put_dur.as_secs_f64() * 1000.0
    );
    println!(
        "  -> Vault Key Search Latency: p50={:.2} µs | p95={:.2} µs | p99={:.2} µs",
        search_p50, search_p95, search_p99
    );

    // Hardened Rubric v2.1 Verification: Audit Chain Integrity & Point-in-time Latency
    let t_kibisis_verify_start = Instant::now();
    let chain_valid = vault.verify_audit_chain(None);
    let kibisis_verify_dur = t_kibisis_verify_start.elapsed();
    assert!(chain_valid);
    let kibisis_verify_ops = n_entities as f64 / kibisis_verify_dur.as_secs_f64();
    println!(
        "  -> Memory Vault Audit Journal: 10,000 events verified in {:.2} ms ({:.0} records/sec)",
        kibisis_verify_dur.as_secs_f64() * 1000.0,
        kibisis_verify_ops
    );

    println!("  -> Contended lease round-trip (8 threads, 10 resources):");
    println!(
        "     Round trips: {} of {} attempts in {:.2} ms ({:.0} round trips/sec, {:.0} attempts/sec)",
        total_acquisitions,
        total_lease_attempts,
        lease_total_dur.as_secs_f64() * 1000.0,
        lease_throughput,
        lease_attempts_per_sec
    );
    println!(
        "     Conflicts rejected while another thread held the lease: {}\n",
        total_conflicts
    );

    // ---------------------------------------------------------
    // 3. ARGUS BENCHMARKS (AUDIT LEDGER & PROVENANCE)
    // ---------------------------------------------------------
    println!("[3/3] Benchmarking ARGUS Tamper-Evident Ledger (SHA-256 Chaining)...");
    let ledger = InMemoryLedger::new();
    let n_events = 10000;

    let t_append_start = Instant::now();
    for i in 0..n_events {
        ledger
            .append(
                "agent.action",
                "autonomous_worker_1",
                &format!(
                    "{{\"action\":\"navigate_edge\",\"step\":{},\"target\":\"zone_b\"}}",
                    i
                ),
            )
            .unwrap();
    }
    let append_dur = t_append_start.elapsed();
    let append_throughput = n_events as f64 / append_dur.as_secs_f64();

    // Full Chain Verification Benchmark
    let t_verify_start = Instant::now();
    let report = ledger.verify_chain().unwrap();
    let verify_dur = t_verify_start.elapsed();
    assert!(report.valid);
    assert_eq!(report.total_events, n_events);

    // Tamper check: append one byte to one event's payload and verify the export again
    const TAMPER_EVENT: u64 = 42;
    println!(
        "  -> Tamper check: appending one byte to event {TAMPER_EVENT} and verifying again..."
    );
    let all_events = ledger.all_events().unwrap();
    let mut corrupted = all_events.clone();
    let tamper_idx = (TAMPER_EVENT - 1) as usize;
    let mut bad_payload = corrupted[tamper_idx].payload.clone();
    bad_payload.push(' ');
    corrupted[tamper_idx].payload = bad_payload;

    // Verify the corrupted export with the ledger's own verifier (positional, fail-closed)
    let t_tamper_start = Instant::now();
    let tampered = VerificationPolicy::default().verify(&corrupted).unwrap();
    let tamper_eval_dur = t_tamper_start.elapsed();
    let tamper_detected = !tampered.valid;
    let reported_event = tampered.first_broken_sequence.expect("tamper not reported");
    assert!(tamper_detected);
    assert_eq!(reported_event, TAMPER_EVENT);

    let verify_ops = n_events as f64 / verify_dur.as_secs_f64();
    let tamper_us = tamper_eval_dur.as_nanos() as f64 / 1000.0;

    println!("  -> Ledger Append Throughput: {:.0} events/sec with SHA-256 chaining ({:.2} ms for 10k events)",
        append_throughput, append_dur.as_secs_f64() * 1000.0
    );
    println!("  -> Full Chain Verification: 10,000 events cryptographically verified in {:.2} ms ({:.0} events/sec verified)",
        verify_dur.as_secs_f64() * 1000.0, verify_ops
    );
    println!("  -> Tamper check: reported at event {} after {:.2} µs (verification stops at the first broken link, so this time grows with the edit's position)",
        reported_event, tamper_us
    );

    // Read after the timed work: /proc/cpuinfo is about 113 KiB on a 48-core machine.
    let host = host::describe();
    println!("\n================================================================================");
    println!("                         PHASE 1 BENCHMARKS COMPLETE                           ");
    println!("          {host}");
    println!("================================================================================");

    if let Some(target_file) = json_path {
        let rep = BenchmarkReport {
            benchmark: "telarch-benchmarks-phase1",
            suite: "Telarch Suite (TCI)",
            host,
            timestamp_utc: clock::utc_now_rfc3339(),
            harpe: HarpeReport {
                small_context: ScenarioReport {
                    items: small_items.len(),
                    input_tokens: small_tokens,
                    budget_tokens: small_budget_tokens,
                    iterations: small_iters,
                    ops_per_sec: small_ops,
                    latency_p50_us: small_p50,
                    latency_p95_us: small_p95,
                    latency_p99_us: small_p99,
                },
                large_context: ScenarioReport {
                    items: large_items.len(),
                    input_tokens: large_tokens,
                    budget_tokens: large_budget_tokens,
                    iterations: large_iters,
                    ops_per_sec: large_ops,
                    latency_p50_us: large_p50,
                    latency_p95_us: large_p95,
                    latency_p99_us: large_p99,
                },
                token_optimization_pct: reduction_pct,
            },
            kibisis: KibisisReport {
                vault_put_entities: n_entities,
                vault_put_duration_ms: vault_put_dur.as_secs_f64() * 1000.0,
                vault_put_ops_per_sec: vault_put_throughput,
                vault_search_iterations: search_iters,
                vault_search_p50_us: search_p50,
                vault_search_p95_us: search_p95,
                vault_search_p99_us: search_p99,
                vault_audit_verify_events: n_entities,
                vault_audit_verify_duration_ms: kibisis_verify_dur.as_secs_f64() * 1000.0,
                vault_audit_verify_ops_per_sec: kibisis_verify_ops,
                concurrency_leases: LeaseReport {
                    label: "contended lease round-trip",
                    threads: num_threads,
                    resources: 10,
                    iters_per_thread,
                    total_attempts: total_lease_attempts,
                    duration_ms: lease_total_dur.as_secs_f64() * 1000.0,
                    round_trips_per_sec: lease_throughput,
                    attempts_per_sec: lease_attempts_per_sec,
                    acquisitions: total_acquisitions,
                    conflict_rejections: total_conflicts,
                },
            },
            argus: ArgusReport {
                append_events: n_events,
                append_duration_ms: append_dur.as_secs_f64() * 1000.0,
                append_ops_per_sec: append_throughput,
                verify_events: n_events,
                verify_duration_ms: verify_dur.as_secs_f64() * 1000.0,
                verify_ops_per_sec: verify_ops,
                tamper_detection_us: tamper_us,
                tamper_edited_event: TAMPER_EVENT,
                tamper_reported_event: reported_event,
                tamper_resistance_pass: true,
            },
        };

        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized = serde_json::to_string_pretty(&rep).expect("Failed to serialize report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!(
            "\n[REPORT] Saved machine-readable benchmark report to: {}",
            target_file
        );
    }
}
