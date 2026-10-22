//! Step 1: Harpe over 100,000 simulated agent turns.
//!
//! Measures tokens removed, budget overruns and time per turn under a 1,200-token budget on
//! synthetic turns (system prompt, user query, policy, memory and 30 low-priority log blocks).
//! It reports tokens, not money: dollar figures would depend on assumed prices.

use std::fs;
use std::path::Path;
use std::time::Instant;

use harpe::{ContextBudgetEngine, ContextItem, ContextSynthesizer, PrunePolicy, TokenBudget};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;

#[derive(Serialize)]
struct KnapsackCostCurveReport {
    benchmark: &'static str,
    suite: &'static str,
    host: &'static str,
    timestamp_utc: String,
    total_turns_simulated: usize,
    unbudgeted_total_tokens: usize,
    harpe_total_tokens: usize,
    token_reduction_ratio_pct: f64,
    budget_overruns: usize,
    total_harpe_time_ms: f64,
    avg_harpe_latency_us: f64,
    throughput_turns_per_sec: f64,
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

    // Reusable template items with variable content to simulate 100k distinct turns
    let mut total_unbudgeted_tokens = 0usize;
    let mut total_harpe_tokens = 0usize;
    let mut budget_overruns = 0usize;

    let start = Instant::now();

    for turn in 0..target_turns {
        let budget = TokenBudget::new(budget_ceiling);

        // Turn components:
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
        let mut noise_items = Vec::with_capacity(30);
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
            noise_items.push(ContextItem::new(
                format!("diag_noise_{}", i),
                noise_content,
                50 + (i as u8 % 40),
            ));
        }

        // Assemble unpruned haystack
        let mut items = Vec::with_capacity(34);
        items.push(sys);
        items.push(user);
        items.push(policy);
        items.push(memory);
        items.extend(noise_items);

        let report = engine
            .synthesize(&items, &budget, PrunePolicy::LowestPriority)
            .expect("Synthesis must succeed");

        let unbudgeted_turn_tokens: usize = items
            .iter()
            .map(|it| {
                // Count using the same heuristic as Harpe
                let words = it.content.split_whitespace().count();
                let chars = it.content.chars().count();
                std::cmp::max(words, chars.div_ceil(4))
            })
            .sum();
        total_unbudgeted_tokens += unbudgeted_turn_tokens;

        if report.consumed_tokens > budget_ceiling {
            budget_overruns += 1;
        }

        total_harpe_tokens += report.consumed_tokens;
    }

    let total_elapsed = start.elapsed();
    let total_ms = total_elapsed.as_secs_f64() * 1000.0;
    let avg_us = (total_elapsed.as_nanos() as f64 / target_turns as f64) / 1000.0;
    let turns_per_sec = target_turns as f64 / total_elapsed.as_secs_f64();
    let reduction_pct =
        (1.0 - (total_harpe_tokens as f64 / total_unbudgeted_tokens as f64)) * 100.0;

    println!("\n--- EXECUTION PERFORMANCE ---");
    println!("  • Turns Processed:           {} turns", target_turns);
    println!(
        "  • Total Execution Time:      {:.2} ms ({:.2} s)",
        total_ms,
        total_elapsed.as_secs_f64()
    );
    println!("  • Average Latency per Turn:  {:.2} µs", avg_us);
    println!(
        "  • Throughput:                {:.0} turns/sec",
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

    let report = KnapsackCostCurveReport {
        benchmark: "telarch-step1-knapsack-cost-curve",
        suite: "Telarch Suite (TCI)",
        host: "Linux x86_64, Intel Core i5-13500 (20 threads)",
        timestamp_utc: clock::utc_now_rfc3339(),
        total_turns_simulated: target_turns,
        unbudgeted_total_tokens: total_unbudgeted_tokens,
        harpe_total_tokens: total_harpe_tokens,
        token_reduction_ratio_pct: reduction_pct,
        budget_overruns,
        total_harpe_time_ms: total_ms,
        avg_harpe_latency_us: avg_us,
        throughput_turns_per_sec: turns_per_sec,
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
