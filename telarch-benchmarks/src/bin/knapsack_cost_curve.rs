//! Step 1: Harpe over 100,000 simulated agent turns.
//!
//! Measures tokens removed, budget overruns and time per turn under a 1,200-token budget on
//! synthetic turns (system prompt, user query, policy, memory and 30 low-priority log blocks).
//! It reports tokens, not money: dollar figures would depend on assumed prices.
//!
//! Only the `synthesize` call is timed as Harpe. Building the 34 items of a turn and counting
//! the unbudgeted baseline are harness work and are timed separately, because one timer around
//! all three made the published per-turn figure about 2.3x Harpe's own cost.

use std::fs;
use std::path::Path;
use std::time::Instant;

use harpe::{ContextBudgetEngine, ContextItem, ContextSynthesizer, PrunePolicy, TokenBudget};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;
#[path = "../host.rs"]
mod host;

#[derive(Serialize)]
struct KnapsackCostCurveReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
    timestamp_utc: String,
    total_turns_simulated: usize,
    unbudgeted_total_tokens: usize,
    harpe_total_tokens: usize,
    token_reduction_ratio_pct: f64,
    budget_overruns: usize,
    /// Sum of the `synthesize` calls only.
    total_harpe_time_ms: f64,
    /// `synthesize` only, averaged per turn.
    avg_harpe_latency_us: f64,
    /// Same quantity as avg_harpe_latency_us under the name the review asked for.
    synthesize_us: f64,
    /// Per turn: building the synthetic items plus counting the unbudgeted baseline. Not Harpe.
    harness_overhead_us: f64,
    /// Wall time of the whole loop, synthesize and harness together.
    wall_time_ms: f64,
    /// Turns per second of `synthesize` alone.
    throughput_turns_per_sec: f64,
    /// Why the reduction is what it is, so the figure is not read as a property of real traffic.
    reduction_note: &'static str,
}

/// The unbudgeted token count of a turn, with the heuristic Harpe uses.
fn baseline_tokens(items: &[ContextItem]) -> usize {
    items
        .iter()
        .map(|it| {
            let words = it.content.split_whitespace().count();
            let chars = it.content.chars().count();
            std::cmp::max(words, chars.div_ceil(4))
        })
        .sum()
}

/// One synthetic turn: four pinned-priority items and 30 low-priority log blocks (~15,000 tokens).
fn build_turn(turn: usize) -> Vec<ContextItem> {
    // 1. System Prompt (High priority: 0) ~50 tokens
    let sys = ContextItem::new(
        "system",
        "You are an autonomous engineering agent with verifiable audit records and deterministic tool execution.",
        0,
    );

    // 2. Active User Query (High priority: 1) ~30 tokens
    let user = ContextItem::new(
        "user",
        format!("Turn #{}: Analyze transaction ledger, isolate anomalies, and verify cryptographic state.", turn),
        1,
    );

    // 3. Retrieved Documentation / Policy (Medium priority: 10) ~150 tokens
    let policy = ContextItem::new(
        "policy",
        "Policy AU-2/AU-3: Append-only SHA-256 chains must be maintained. Do not commit unverified writes. Fencing tokens strictly required for shared locks.",
        10,
    );

    // 4. Memory Context (Medium priority: 15) ~120 tokens
    let memory = ContextItem::new(
        "memory",
        "Previous recall: Controller deployed version 1.4.2 at epoch 1700000000. Verified 45 prior operations without regression.",
        15,
    );

    // 5. Unbudgeted Historical Noise & Deep Haystack (Low priority: 50..90) ~15,000 tokens
    // Typical unbudgeted agent multi-turn accumulation: raw git diffs, stack traces, full-file reads, API schemas
    let mut items = Vec::with_capacity(34);
    items.push(sys);
    items.push(user);
    items.push(policy);
    items.push(memory);
    for i in 0..30 {
        let noise_content = format!(
            "Historical execution log and telemetry dump block {}: thread_id={} heap_alloc_bytes={} io_ops={} socket_poll_result=NOMINAL_READY_STATE_OK_ACK. \
             Stack trace frame 0x7fff{:08x} in module kernel_dispatch symbol _rt_exec_worker_{}. \
             Memory subsystem reports cache miss ratio 0.002, dirty pages 12, writeback active, transaction journal synced to disk. \
             Prior compiler output: warnings=0 errors=0 code_generation=complete optimization_level=release codegen_units=1. \
             Diff chunk @@ -102,15 +102,18 @@ struct WorkerRegistry {{ status: Active, leases: [{}] }}. \
             Network diagnostic stream: rx_packets=948271 tx_packets=812938 rx_drop=0 tx_drop=0 rx_err=0 tx_err=0 latency_p99_ns=14820.",
            i, 1000 + (turn % 32), 40960 + (i * 1024), 200 + i, 0x1000 + i, i, i,
        );
        items.push(ContextItem::new(
            format!("diag_noise_{}", i),
            noise_content,
            50 + (i as u8 % 40),
        ));
    }
    items
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================");
    println!("  TELARCH SUITE: STEP 1 - HARPE KNAPSACK COST-CURVE BENCHMARK   ");
    println!("  Simulating 100,000 Autonomous Agent Invocation Turns          ");
    println!("================================================================");

    let engine = ContextBudgetEngine::default();
    let target_turns = 100_000;
    let budget_ceiling = 1_200; // Strict 1,200 token budget per prompt turn

    let mut total_unbudgeted_tokens = 0usize;
    let mut total_harpe_tokens = 0usize;
    let mut budget_overruns = 0usize;
    let mut harpe_ns: u128 = 0;
    let mut harness_ns: u128 = 0;

    let wall_start = Instant::now();

    for turn in 0..target_turns {
        let t_build = Instant::now();
        let budget = TokenBudget::new(budget_ceiling);
        let items = build_turn(turn);

        let t_synth = Instant::now();
        let report = engine
            .synthesize(&items, &budget, PrunePolicy::LowestPriority)
            .expect("Synthesis must succeed");
        let t_done = Instant::now();
        // black_box keeps the optimizer from dropping the call when the result looks unused.
        total_harpe_tokens += std::hint::black_box(report.consumed_tokens);

        if report.consumed_tokens > budget_ceiling {
            budget_overruns += 1;
        }
        total_unbudgeted_tokens += baseline_tokens(&items);

        harpe_ns += (t_done - t_synth).as_nanos();
        harness_ns += (t_synth - t_build).as_nanos() + t_done.elapsed().as_nanos();
    }

    let wall_elapsed = wall_start.elapsed();
    let wall_ms = wall_elapsed.as_secs_f64() * 1000.0;
    let total_ms = harpe_ns as f64 / 1_000_000.0;
    let avg_us = harpe_ns as f64 / target_turns as f64 / 1000.0;
    let harness_us = harness_ns as f64 / target_turns as f64 / 1000.0;
    let turns_per_sec = target_turns as f64 / (harpe_ns as f64 / 1e9);
    let reduction_pct =
        (1.0 - (total_harpe_tokens as f64 / total_unbudgeted_tokens as f64)) * 100.0;
    // The 30 log blocks are the same size every turn, so the reduction is fixed by the budget
    // and the input size. It says nothing about how much a real trajectory would shrink.
    let reduction_note = "Synthetic workload: the reduction follows from the 1,200-token budget \
        over a fixed ~5,200-token input, so it is a property of the budget/input ratio, not of \
        Harpe's selection quality on real traffic";

    println!("\n--- EXECUTION PERFORMANCE ---");
    println!("  • Turns Processed:           {} turns", target_turns);
    println!(
        "  • Harpe synthesize time:     {:.2} ms ({:.2} s) of {:.2} ms wall",
        total_ms,
        total_ms / 1000.0,
        wall_ms
    );
    println!("  • synthesize per turn:       {:.2} µs", avg_us);
    println!(
        "  • Harness overhead per turn: {:.2} µs (item construction + baseline count, not Harpe)",
        harness_us
    );
    println!(
        "  • Throughput (synthesize):   {:.0} turns/sec",
        turns_per_sec
    );
    println!(
        "  • Budget Overruns:           {} ({:.2}%)",
        budget_overruns,
        budget_overruns as f64 * 100.0 / target_turns as f64
    );

    println!("\n--- TOKEN REDUCTION ---");
    println!(
        "  • Unbudgeted Context Tokens: {} tokens ({:.1} avg/turn)",
        total_unbudgeted_tokens,
        total_unbudgeted_tokens as f64 / target_turns as f64
    );
    println!(
        "  • Harpe Synthesized Tokens:  {} tokens ({:.1} avg/turn)",
        total_harpe_tokens,
        total_harpe_tokens as f64 / target_turns as f64
    );
    println!(
        "  • Total Tokens Saved:        {} tokens",
        total_unbudgeted_tokens - total_harpe_tokens
    );
    println!("  • Token Reduction Ratio:     {:.2}%", reduction_pct);
    println!("  • Note: {}", reduction_note);

    let report = KnapsackCostCurveReport {
        benchmark: "telarch-step1-knapsack-cost-curve",
        suite: "Telarch Suite (TCI)",
        host: host::describe(),
        timestamp_utc: clock::utc_now_rfc3339(),
        total_turns_simulated: target_turns,
        unbudgeted_total_tokens: total_unbudgeted_tokens,
        harpe_total_tokens: total_harpe_tokens,
        token_reduction_ratio_pct: reduction_pct,
        budget_overruns,
        total_harpe_time_ms: total_ms,
        avg_harpe_latency_us: avg_us,
        synthesize_us: avg_us,
        harness_overhead_us: harness_us,
        wall_time_ms: wall_ms,
        throughput_turns_per_sec: turns_per_sec,
        reduction_note,
    };

    if let Some(target_file) = json_path {
        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized =
            serde_json::to_string_pretty(&report).expect("Failed to serialize cost report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!(
            "\n[REPORT] Saved machine-readable report to: {}",
            target_file
        );
    }
}
