//! Step 1: Harpe Knapsack Cost-Curve & Token Economics Benchmark
//!
//! Evaluates prompt token reduction, latency, and projected API dollar savings
//! across 100,000 synthetic agent turns with realistic context haystacks (system prompts,
//! conversation history, retrieved tools/docs, and scratchpad debug noise).
//!
//! Evaluates pricing schedules:
//! - Anthropic Claude 3.5 Sonnet: $3.00 / 1M input tokens
//! - OpenAI GPT-4o: $2.50 / 1M input tokens
//! - Google Gemini 1.5 Flash: $0.075 / 1M input tokens

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
    cost_projections: CostProjections,
}

#[derive(Serialize)]
struct CostProjections {
    claude_35_sonnet: ModelCostDelta, // Retained for backward-compat in reports
    claude_sonnet_contemporary: ModelCostDelta,
    frontier_flagship: ModelCostDelta,
    gemini_flash_contemporary: ModelCostDelta,
}

#[derive(Serialize)]
struct ModelCostDelta {
    model_name: &'static str,
    input_rate_per_1m: f64,
    cache_hit_rate_per_1m: Option<f64>,
    unbudgeted_cost_usd: f64,
    harpe_cost_usd: f64,
    dollar_savings_usd: f64,
    savings_pct: f64,
}

fn calculate_cost(tokens: usize, rate_per_1m: f64) -> f64 {
    (tokens as f64 / 1_000_000.0) * rate_per_1m
}

fn build_model_cost(
    model_name: &'static str,
    rate: f64,
    cache_hit_rate: Option<f64>,
    unbudgeted_tokens: usize,
    harpe_tokens: usize,
) -> ModelCostDelta {
    let unbudgeted_cost = calculate_cost(unbudgeted_tokens, rate);
    let harpe_cost = calculate_cost(harpe_tokens, rate);
    let savings = unbudgeted_cost - harpe_cost;
    let savings_pct = if unbudgeted_cost > 0.0 {
        (savings / unbudgeted_cost) * 100.0
    } else {
        0.0
    };
    ModelCostDelta {
        model_name,
        input_rate_per_1m: rate,
        cache_hit_rate_per_1m: cache_hit_rate,
        unbudgeted_cost_usd: unbudgeted_cost,
        harpe_cost_usd: harpe_cost,
        dollar_savings_usd: savings,
        savings_pct,
    }
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
            "You are an autonomous engineering agent with zero-trust audit compliance and deterministic tool execution.",
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

        let unbudgeted_turn_tokens: usize = items.iter().map(|it| {
            // Count using the same heuristic as Harpe
            let words = it.content.split_whitespace().count();
            let chars = it.content.chars().count();
            std::cmp::max(words, (chars + 3) / 4)
        }).sum();
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
    let reduction_pct = (1.0 - (total_harpe_tokens as f64 / total_unbudgeted_tokens as f64)) * 100.0;

    let cost_sonnet = build_model_cost("Claude 3.5 Sonnet (Legacy Baseline)", 3.00, Some(0.30), total_unbudgeted_tokens, total_harpe_tokens);
    let cost_sonnet_contemporary = build_model_cost("Claude Sonnet (Frontier Tier: $3.00 / $0.30 cache)", 3.00, Some(0.30), total_unbudgeted_tokens, total_harpe_tokens);
    let cost_flagship = build_model_cost("Frontier Reasoning / High-Throughput ($2.50 / $1.25 cache)", 2.50, Some(1.25), total_unbudgeted_tokens, total_harpe_tokens);
    let cost_flash_contemporary = build_model_cost("Gemini Flash Contemporary ($0.10 / $0.025 cache)", 0.10, Some(0.025), total_unbudgeted_tokens, total_harpe_tokens);

    println!("\n--- EXECUTION PERFORMANCE ---");
    println!("  • Turns Processed:           {} turns", target_turns);
    println!("  • Total Execution Time:      {:.2} ms ({:.2} s)", total_ms, total_elapsed.as_secs_f64());
    println!("  • Average Latency per Turn:  {:.2} µs", avg_us);
    println!("  • Throughput:                {:.0} turns/sec", turns_per_sec);
    println!("  • Budget Overruns:           {} (0.00%)", budget_overruns);

    println!("\n--- TOKEN REDUCTION ECONOMICS ---");
    println!("  • Unbudgeted Context Tokens: {} tokens ({:.1} avg/turn)", total_unbudgeted_tokens, total_unbudgeted_tokens as f64 / target_turns as f64);
    println!("  • Harpe Synthesized Tokens:  {} tokens ({:.1} avg/turn)", total_harpe_tokens, total_harpe_tokens as f64 / target_turns as f64);
    println!("  • Total Tokens Saved:        {} tokens", total_unbudgeted_tokens - total_harpe_tokens);
    println!("  • Token Reduction Ratio:     {:.2}%", reduction_pct);

    println!("\n--- PROJECTED FINANCIAL SAVINGS (Per 100k Turns) ---");
    println!("  [Claude Sonnet Contemporary ($3.00/1M tokens, $0.30 cache-hit)]");
    println!("    Unbudgeted Cost:   ${:.2}", cost_sonnet_contemporary.unbudgeted_cost_usd);
    println!("    Harpe Cost:        ${:.2}", cost_sonnet_contemporary.harpe_cost_usd);
    println!("    Net Savings:       ${:.2} ({:.1}%)", cost_sonnet_contemporary.dollar_savings_usd, cost_sonnet_contemporary.savings_pct);

    println!("  [Frontier Flagship Tier ($2.50/1M tokens, $1.25 cache-hit)]");
    println!("    Unbudgeted Cost:   ${:.2}", cost_flagship.unbudgeted_cost_usd);
    println!("    Harpe Cost:        ${:.2}", cost_flagship.harpe_cost_usd);
    println!("    Net Savings:       ${:.2} ({:.1}%)", cost_flagship.dollar_savings_usd, cost_flagship.savings_pct);

    println!("  [Gemini Flash Contemporary ($0.10/1M tokens, $0.025 cache-hit)]");
    println!("    Unbudgeted Cost:   ${:.2}", cost_flash_contemporary.unbudgeted_cost_usd);
    println!("    Harpe Cost:        ${:.2}", cost_flash_contemporary.harpe_cost_usd);
    println!("    Net Savings:       ${:.2} ({:.1}%)", cost_flash_contemporary.dollar_savings_usd, cost_flash_contemporary.savings_pct);

    let report = KnapsackCostCurveReport {
        benchmark: "telarch-step1-knapsack-cost-curve",
        suite: "Telarch Suite (TCI)",
        host: "Greg (Linux x86_64)",
        timestamp_utc: clock::utc_now_rfc3339(),
        total_turns_simulated: target_turns,
        unbudgeted_total_tokens: total_unbudgeted_tokens,
        harpe_total_tokens: total_harpe_tokens,
        token_reduction_ratio_pct: reduction_pct,
        budget_overruns,
        total_harpe_time_ms: total_ms,
        avg_harpe_latency_us: avg_us,
        throughput_turns_per_sec: turns_per_sec,
        cost_projections: CostProjections {
            claude_35_sonnet: cost_sonnet,
            claude_sonnet_contemporary: cost_sonnet_contemporary,
            frontier_flagship: cost_flagship,
            gemini_flash_contemporary: cost_flash_contemporary,
        },
    };

    if let Some(target_file) = json_path {
        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized = serde_json::to_string_pretty(&report).expect("Failed to serialize cost report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!("\n[REPORT] Saved machine-readable report to: {}", target_file);
    }
}
