//! Step 2: Adversarial Multi-Worker Contention & Split-Brain Resistance Harness
//!
//! Evaluates Kibisis distributed single-writer multi-reader (SWMR) lease coordination
//! under heavy contention across 16 and 32 parallel worker threads.
//!
//! Verifies:
//! 1. Monotonic fencing token progression (zero token rollbacks or duplicate tokens)
//! 2. Zero split-brain dual-ownership states across competing workers
//! 3. Successful acquire->release cycles per second and lease decisions per second, reported
//!    separately, with the rejection rate beside them
//! 4. That an abandoned lease is reclaimable once its TTL has elapsed
//!
//! Lease turnover in the contention scenarios comes from explicit `release` calls, not from
//! expiry: a rejected `acquire` is a cheap map probe, so attempts/s must not be read as
//! lease operations. The expiry scenario is the one place the TTL matters.

use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use kibisis::{InMemoryLeaseCoordinator, LeaseCoordinator};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;
#[path = "../host.rs"]
mod host;

#[derive(Serialize)]
struct ContentionReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
    timestamp_utc: String,
    scenario_16_workers: WorkerContentionSummary,
    scenario_32_workers: WorkerContentionSummary,
    lease_expiry_turnover: ExpiryTurnoverSummary,
}

#[derive(Serialize)]
struct WorkerContentionSummary {
    worker_count: usize,
    total_attempts: usize,
    successful_leases: usize,
    conflicts_blocked: usize,
    split_brain_violations: usize,
    fencing_token_rollbacks: usize,
    highest_fencing_token: u64,
    elapsed_ms: f64,
    /// Requested TTL. The in-process coordinator keeps deadlines in whole seconds and rounds up,
    /// so this is a value it honours as given rather than one it silently widens.
    lease_ttl_requested_ms: u64,
    /// How a held lease became free in this scenario.
    turnover_mechanism: &'static str,
    /// Successful acquire -> work -> release cycles per second. This is the lease rate.
    lease_cycles_per_sec: f64,
    /// Every answered `acquire` call per second, rejections included.
    lease_decisions_per_sec: f64,
    /// conflicts / attempts.
    rejection_rate: f64,
    /// Deprecated alias of `lease_decisions_per_sec`, kept so older readers of this report still
    /// parse. It counts rejected attempts, so it is not a lease-operation rate.
    throughput_ops_per_sec: f64,
    /// elapsed / attempts: mean time per answered `acquire`, rejections included.
    avg_contention_latency_us: f64,
    audit_passed: bool,
}

#[derive(Serialize)]
struct ExpiryTurnoverSummary {
    lease_ttl_requested_ms: u64,
    /// Time from the abandoned acquire until another holder's acquire succeeded.
    recovery_ms: f64,
    /// Rejections the waiting holder received before the TTL ran out.
    rejected_before_expiry: usize,
    /// The successor's fencing token is above the abandoned holder's.
    fencing_token_advanced: bool,
    recovered: bool,
}

fn run_worker_contention_scenario(
    worker_count: usize,
    total_rounds_per_worker: usize,
) -> WorkerContentionSummary {
    println!("\n--------------------------------------------------------");
    println!(
        "  RUNNING SCENARIO: {} CONCURRENT WORKERS ({} CYCLES/WORKER)",
        worker_count, total_rounds_per_worker
    );
    println!("--------------------------------------------------------");

    let coordinator = Arc::new(std::sync::Mutex::new(InMemoryLeaseCoordinator::new()));
    let resource_name = "global_orchestration_bus";
    // The coordinator rounds deadlines up to whole seconds (kibisis lease_deadline), so a 50 ms
    // request would be stored as a 1 s deadline anyway. Ask for what it can honour. Turnover
    // here comes from release, so the TTL only matters if a holder dies mid-cycle.
    let lease_ttl = Duration::from_secs(1);

    let total_attempts = Arc::new(AtomicUsize::new(0));
    let successful_leases = Arc::new(AtomicUsize::new(0));
    let conflicts_blocked = Arc::new(AtomicUsize::new(0));
    let split_brain_violations = Arc::new(AtomicUsize::new(0));
    let fencing_token_rollbacks = Arc::new(AtomicUsize::new(0));

    // Shared state to verify single-ownership invariant
    let active_holder = Arc::new(std::sync::Mutex::new(Option::<String>::None));
    let last_fencing_token = Arc::new(AtomicU64::new(0));
    let start_signal = Arc::new(AtomicBool::new(false));

    let mut handles = Vec::with_capacity(worker_count);
    let t_start = Instant::now();

    for w_idx in 0..worker_count {
        let coord_clone = Arc::clone(&coordinator);
        let attempts_clone = Arc::clone(&total_attempts);
        let success_clone = Arc::clone(&successful_leases);
        let conflicts_clone = Arc::clone(&conflicts_blocked);
        let violations_clone = Arc::clone(&split_brain_violations);
        let rollbacks_clone = Arc::clone(&fencing_token_rollbacks);
        let holder_clone = Arc::clone(&active_holder);
        let token_clone = Arc::clone(&last_fencing_token);
        let signal_clone = Arc::clone(&start_signal);
        let worker_id = format!("worker_{:02}", w_idx);

        let handle = thread::spawn(move || {
            // Spin-wait for synchronous release of all threads
            while !signal_clone.load(Ordering::Acquire) {
                std::hint::spin_loop();
            }

            for _ in 0..total_rounds_per_worker {
                attempts_clone.fetch_add(1, Ordering::Relaxed);

                let acquire_res = {
                    let mut lock = coord_clone.lock().unwrap();
                    lock.acquire(resource_name, &worker_id, lease_ttl)
                };

                match acquire_res {
                    Ok(lease) => {
                        success_clone.fetch_add(1, Ordering::Relaxed);

                        // 1. Verify monotonic fencing token progression
                        let prev_token = token_clone.load(Ordering::Acquire);
                        if lease.fencing_token <= prev_token {
                            rollbacks_clone.fetch_add(1, Ordering::Relaxed);
                        }
                        token_clone.store(lease.fencing_token, Ordering::Release);

                        // 2. Critical Section: Check single-holder invariant
                        {
                            let mut current = holder_clone.lock().unwrap();
                            if let Some(existing_worker) = current.as_ref() {
                                if existing_worker != &worker_id {
                                    // Violation: Two different workers simultaneously believe they hold the lease!
                                    violations_clone.fetch_add(1, Ordering::Relaxed);
                                }
                            }
                            *current = Some(worker_id.clone());
                        }

                        // Simulated micro-work (bounded critical section)
                        std::thread::yield_now();

                        // 3. Clear Critical Section
                        {
                            let mut current = holder_clone.lock().unwrap();
                            *current = None;
                        }

                        // 4. Release Lease
                        let mut lock = coord_clone.lock().unwrap();
                        let _ = lock.release(&lease);
                    }
                    Err(_) => {
                        // Conflict gracefully caught and blocked by Kibisis
                        conflicts_clone.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        });
        handles.push(handle);
    }

    // Release workers simultaneously
    start_signal.store(true, Ordering::Release);

    for h in handles {
        h.join().unwrap();
    }

    let elapsed = t_start.elapsed();
    let secs = elapsed.as_secs_f64();
    let elapsed_ms = secs * 1000.0;
    let attempts = total_attempts.load(Ordering::Relaxed);
    let successes = successful_leases.load(Ordering::Relaxed);
    let conflicts = conflicts_blocked.load(Ordering::Relaxed);
    let violations = split_brain_violations.load(Ordering::Relaxed);
    let rollbacks = fencing_token_rollbacks.load(Ordering::Relaxed);
    let highest_token = last_fencing_token.load(Ordering::Relaxed);
    // A rejected acquire is a HashMap probe under a lock, not a lease operation. The two rates
    // are published side by side so a headline cannot pass one off as the other.
    let decisions_per_sec = attempts as f64 / secs;
    let cycles_per_sec = successes as f64 / secs;
    let rejection_rate = conflicts as f64 / attempts.max(1) as f64;
    let avg_latency_us = (elapsed.as_nanos() as f64 / attempts as f64) / 1000.0;
    let passed = violations == 0 && rollbacks == 0 && successes > 0;

    println!("  • Total Acquire Attempts:    {}", attempts);
    println!(
        "  • Successful Acquisitions:   {} ({:.1}%)",
        successes,
        (successes as f64 / attempts as f64) * 100.0
    );
    println!(
        "  • Conflicts Blocked:         {} ({:.1}%)",
        conflicts,
        rejection_rate * 100.0
    );
    println!(
        "  • Split-Brain Violations:    {} (Invariant 100% Preserved)",
        violations
    );
    println!(
        "  • Fencing Token Rollbacks:   {} (Strictly Monotonic)",
        rollbacks
    );
    println!("  • Highest Fencing Token:     {}", highest_token);
    println!(
        "  • Execution Duration:        {:.2} ms ({:.2} s)",
        elapsed_ms, secs
    );
    println!(
        "  • Lease cycles/s (acquire->release): {:.0}",
        cycles_per_sec
    );
    println!(
        "  • Lease decisions/s (incl. rejections): {:.0}",
        decisions_per_sec
    );
    println!(
        "  • Rejection rate:            {:.1}% (requested TTL {:?}; turnover by release)",
        rejection_rate * 100.0,
        lease_ttl
    );
    println!("  • Mean decision latency:     {:.2} µs", avg_latency_us);
    println!(
        "  • Scenario Verdict:          {}",
        if passed {
            "PASSED (VERIFIED)"
        } else {
            "FAILED"
        }
    );

    assert_eq!(violations, 0, "Split-brain violations detected!");
    assert_eq!(rollbacks, 0, "Fencing token monotonicity violated!");

    WorkerContentionSummary {
        worker_count,
        total_attempts: attempts,
        successful_leases: successes,
        conflicts_blocked: conflicts,
        split_brain_violations: violations,
        fencing_token_rollbacks: rollbacks,
        highest_fencing_token: highest_token,
        elapsed_ms,
        lease_ttl_requested_ms: lease_ttl.as_millis() as u64,
        turnover_mechanism: "explicit release by the holder",
        lease_cycles_per_sec: cycles_per_sec,
        lease_decisions_per_sec: decisions_per_sec,
        rejection_rate,
        throughput_ops_per_sec: decisions_per_sec,
        avg_contention_latency_us: avg_latency_us,
        audit_passed: passed,
    }
}

/// One holder acquires and never releases; a second holder polls until the TTL frees the
/// resource. This is the only path where the coordinator's clock decides turnover, so it is
/// measured on its own instead of being folded into the contention rates above.
fn run_expiry_turnover_scenario() -> ExpiryTurnoverSummary {
    println!("\n--------------------------------------------------------");
    println!("  RUNNING SCENARIO: ABANDONED LEASE RECOVERED BY TTL EXPIRY");
    println!("--------------------------------------------------------");

    let mut coordinator = InMemoryLeaseCoordinator::new();
    let resource = "abandoned_bus";
    let lease_ttl = Duration::from_secs(1);
    let abandoned = coordinator
        .acquire(resource, "worker_crashed", lease_ttl)
        .expect("first acquire on a free resource must succeed");

    let t0 = Instant::now();
    let mut rejected = 0usize;
    let successor = loop {
        match coordinator.acquire(resource, "worker_successor", lease_ttl) {
            Ok(lease) => break Some(lease),
            Err(_) => {
                rejected += 1;
                // Whole-second deadlines: polling faster than this only burns CPU.
                thread::sleep(Duration::from_millis(5));
                // Fail closed if the TTL is never honoured; 3 x TTL is well past the
                // one-second round-up.
                if t0.elapsed() > lease_ttl * 3 {
                    break None;
                }
            }
        }
    };
    let recovery = t0.elapsed();
    let recovered = successor.is_some();
    let fencing_token_advanced = successor
        .as_ref()
        .is_some_and(|s| s.fencing_token > abandoned.fencing_token);

    println!("  • Requested TTL:             {:?}", lease_ttl);
    println!(
        "  • Recovered after:           {:.1} ms ({} rejections while held)",
        recovery.as_secs_f64() * 1000.0,
        rejected
    );
    println!("  • Fencing token advanced:    {}", fencing_token_advanced);
    assert!(
        recovered,
        "an abandoned lease must become acquirable after its TTL"
    );
    assert!(
        fencing_token_advanced,
        "the successor must get a higher fencing token"
    );

    ExpiryTurnoverSummary {
        lease_ttl_requested_ms: lease_ttl.as_millis() as u64,
        recovery_ms: recovery.as_secs_f64() * 1000.0,
        rejected_before_expiry: rejected,
        fencing_token_advanced,
        recovered,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================");
    println!("  TELARCH SUITE: STEP 2 - ADVERSARIAL CONTENTION HARNESS        ");
    println!("  Evaluating SWMR Lease Coordination & Split-Brain Invariants   ");
    println!("================================================================");

    let summary_16 = run_worker_contention_scenario(16, 2_000); // 32,000 attempts
    let summary_32 = run_worker_contention_scenario(32, 1_000); // 32,000 attempts
    let expiry = run_expiry_turnover_scenario();

    println!("\n================================================================");
    println!("  ALL ADVERSARIAL CONTENTION SCENARIOS COMPLETED & VERIFIED     ");
    println!("================================================================");

    let report = ContentionReport {
        benchmark: "telarch-step2-adversarial-contention",
        suite: "Telarch Suite (TCI)",
        host: host::describe(),
        timestamp_utc: clock::utc_now_rfc3339(),
        scenario_16_workers: summary_16,
        scenario_32_workers: summary_32,
        lease_expiry_turnover: expiry,
    };

    if let Some(target_file) = json_path {
        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized =
            serde_json::to_string_pretty(&report).expect("Failed to serialize contention report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!(
            "\n[REPORT] Saved machine-readable report to: {}",
            target_file
        );
    }
}
