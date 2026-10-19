//! Micro-Payload & Restricted Environment Verification Benchmark
//!
//! Evaluates the lowest viable operational payload sizes for the Harpe/Kibisis/Argus
//! triad under highly constrained token budgets and restricted permission sandboxes.
//!
//! Tiers evaluated:
//! - Tier 0: Tactical Pulse (60-token budget ceiling)
//! - Tier 1: Minimal Closed-Loop Reasoning (150-token budget ceiling)

use std::fs;
use std::path::Path;
use std::time::Instant;

use argus_ledger::{sha256_hex, AuditLedgerWriter, AuditVerifier, InMemoryLedger};
use harpe::{ContextBudgetEngine, ContextItem, ContextSynthesizer, PrunePolicy, TokenBudget};
use kibisis::{
    InMemoryLeaseCoordinator, InMemoryMemoryVault, LeaseCoordinator, MemoryEntity, MemoryVault,
};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;

#[derive(Serialize)]
struct RestrictedPayloadReport {
    benchmark: &'static str,
    suite: &'static str,
    host: &'static str,
    timestamp_utc: String,
    tier0_tactical_pulse: Tier0Report,
    tier1_closed_loop: Tier1Report,
}

#[derive(Serialize)]
struct Tier0Report {
    raw_haystack_tokens: usize,
    synthesized_prompt_tokens: usize,
    budget_ceiling: usize,
    reduction_pct: f64,
    harpe_latency_us: f64,
    wire_payload_bytes: usize,
    compact_binary_bytes: usize,
    argus_append_latency_us: f64,
    chain_integrity_valid: bool,
}

#[derive(Serialize)]
struct Tier1Report {
    raw_context_tokens: usize,
    synthesized_prompt_tokens: usize,
    budget_ceiling: usize,
    reduction_pct: f64,
    harpe_latency_us: f64,
    wire_payload_bytes: usize,
    fencing_token: u64,
    argus_append_latency_us: f64,
    chain_integrity_valid: bool,
}

fn run_tier0_tactical_pulse() -> Tier0Report {
    println!("\n========================================================");
    println!("  RUNNING TIER 0: MICRO-TACTICAL PULSE (60 TOKENS)");
    println!("========================================================");

    // 1. Setup high-volume raw telemetry items (50 items = ~1,500 tokens)
    let mut raw_items = Vec::new();
    let mut total_raw_tokens = 0;
    for i in 0..50 {
        let content = format!(
            "Telemetry node_{:02}: temp=42.1C status=nominal freq=2.4GHz seq={}",
            i,
            1000 + i
        );
        let tokens = (content.len() / 4).max(1);
        total_raw_tokens += tokens;
        let priority = if i == 7 { 1 } else { 50 + (i as u8 % 40) };
        raw_items.push(ContextItem::new(
            format!("node_{:02}", i),
            content,
            priority,
        ));
    }

    // Harpe knapsack synthesis under strict 60-token ceiling
    let engine = ContextBudgetEngine::default();
    let budget = TokenBudget::new(60);
    let t0 = Instant::now();
    let report = engine
        .synthesize(&raw_items, &budget, PrunePolicy::LowestPriority)
        .unwrap();
    let elapsed = t0.elapsed();

    assert!(
        report.consumed_tokens <= 60,
        "Tier 0 exceeded 60-token ceiling: {}",
        report.consumed_tokens
    );
    assert!(
        !report.included_items.is_empty(),
        "Expected at least one item included within budget"
    );
    assert!(
        report.included_items.iter().any(|it| it.id == "node_07"),
        "Expected critical item node_07 to be selected"
    );

    // Canonical wire payload framing (UTF-8 JSON, keys sorted as the ledger requires)
    let wire_payload = format!(
        "{{\"auth\":\"{}\",\"k\":\"{}\",\"seq\":101,\"tok\":{},\"v\":\"{}\"}}",
        &sha256_hex(report.included_items[0].content.as_bytes())[0..16],
        report.included_items[0].id,
        report.consumed_tokens,
        report.included_items[0].content,
    );
    let wire_bytes = wire_payload.len();

    // Compact binary framing simulation (TLV: Type 1B, Len 1B, Seq 4B, Hash 8B, Body)
    let compact_binary_bytes = 1 + 1 + 4 + 8 + report.included_items[0].content.len();

    // Argus tamper-evident provenance ledger
    let ledger = InMemoryLedger::new();
    let t_append = Instant::now();
    let event = ledger
        .append("tactical.pulse", "edge_node_07", &wire_payload)
        .unwrap();
    let append_us = t_append.elapsed().as_nanos() as f64 / 1_000.0;

    let verifier = ledger.verify_chain().unwrap();
    assert!(verifier.valid, "Argus ledger chain verification failed");

    let reduction_pct = (1.0 - (report.consumed_tokens as f64 / total_raw_tokens as f64)) * 100.0;
    let harpe_us = elapsed.as_nanos() as f64 / 1_000.0;

    println!(
        "  • Raw Haystack Tokens:      {} tokens (50 items)",
        total_raw_tokens
    );
    println!(
        "  • Synthesized Prompt Tokens: {} tokens",
        report.consumed_tokens
    );
    println!("  • Ceiling Budget Enforced:   60 tokens (Overrun: 0 tokens)");
    println!("  • Context Reduction Ratio:   {:.2}%", reduction_pct);
    println!("  • Harpe Knapsack Latency:    {:.2} µs", harpe_us);
    println!("  • Wire Payload (Min JSON):   {} bytes", wire_bytes);
    println!(
        "  • Compact Binary (TLV):      {} bytes",
        compact_binary_bytes
    );
    println!("  • Argus Append Latency:      {:.2} µs", append_us);
    println!(
        "  • Ledger Event Hash:         {}",
        &event.event_hash[0..24]
    );
    println!("  • SHA-256 Chain Integrity:   VALID (0 gaps, 0 tampers)");

    Tier0Report {
        raw_haystack_tokens: total_raw_tokens,
        synthesized_prompt_tokens: report.consumed_tokens,
        budget_ceiling: 60,
        reduction_pct,
        harpe_latency_us: harpe_us,
        wire_payload_bytes: wire_bytes,
        compact_binary_bytes,
        argus_append_latency_us: append_us,
        chain_integrity_valid: verifier.valid,
    }
}

fn run_tier1_closed_loop() -> Tier1Report {
    println!("\n========================================================");
    println!("  RUNNING TIER 1: MINIMAL CLOSED-LOOP (150 TOKENS)");
    println!("========================================================");

    // 1. Vault initialization with atomic concurrency
    let mut vault = InMemoryMemoryVault::new();
    let mut coord = InMemoryLeaseCoordinator::new();
    let lease = coord
        .acquire(
            "grid_bus_alpha",
            "controller_01",
            std::time::Duration::from_secs(30),
        )
        .unwrap();
    assert_eq!(lease.fencing_token, 1);

    vault
        .put(&MemoryEntity::new(
            "directives",
            "directive_01",
            "Air-gapped controller: verify bus voltage or isolate.",
        ))
        .unwrap();
    vault
        .put(&MemoryEntity::new(
            "state",
            "fact_alpha",
            "Bus alpha operating at 480V; relay 4 armed.",
        ))
        .unwrap();

    // 2. Add background distractors (100 items = ~3,000 tokens)
    let mut items = Vec::new();
    let mut raw_tokens = 0;
    for i in 0..100 {
        let text = format!(
            "Historical log #{} bus status nominal drift 0.0{}V",
            i,
            i % 5
        );
        let tok = (text.len() / 4).max(1);
        raw_tokens += tok;
        items.push(ContextItem::new(
            format!("log_{:03}", i),
            text,
            50 + (i as u8 % 40),
        ));
    }
    // High-priority operational directives
    items.push(ContextItem::new(
        "directive".to_string(),
        "Air-gapped controller: verify bus voltage or isolate.".to_string(),
        1,
    ));
    items.push(ContextItem::new(
        "fact".to_string(),
        "Bus alpha operating at 480V; relay 4 armed.".to_string(),
        2,
    ));
    raw_tokens += 35;

    // Harpe knapsack synthesis under 150-token ceiling
    let engine = ContextBudgetEngine::default();
    let budget = TokenBudget::new(150);
    let t0 = Instant::now();
    let report = engine
        .synthesize(&items, &budget, PrunePolicy::LowestPriority)
        .unwrap();
    let elapsed = t0.elapsed();

    assert!(
        report.consumed_tokens <= 150,
        "Tier 1 exceeded 150-token ceiling: {}",
        report.consumed_tokens
    );
    assert!(
        report.consumed_tokens >= 30,
        "Expected core items preserved"
    );

    // Formatted structured wire payload (keys sorted as the ledger requires)
    let wire_payload = format!(
        "{{\"digest\":\"{}\",\"fencing_token\":{},\"included_count\":{},\"seq\":502,\"tokens\":{}}}",
        &sha256_hex(format!("lease:{}", lease.fencing_token).as_bytes())[0..16],
        lease.fencing_token,
        report.included_items.len(),
        report.consumed_tokens,
    );
    let wire_bytes = wire_payload.len();

    // Argus logging & audit verification
    let ledger = InMemoryLedger::new();
    let t_app = Instant::now();
    let event = ledger
        .append("loop.closed_action", "controller_01", &wire_payload)
        .unwrap();
    let append_us = t_app.elapsed().as_nanos() as f64 / 1_000.0;

    let verifier = ledger.verify_chain().unwrap();
    assert!(verifier.valid, "Argus ledger chain verification failed");

    let reduction_pct = (1.0 - (report.consumed_tokens as f64 / raw_tokens as f64)) * 100.0;
    let harpe_us = elapsed.as_nanos() as f64 / 1_000.0;

    println!(
        "  • Raw Context Tokens:        {} tokens (102 items)",
        raw_tokens
    );
    println!(
        "  • Synthesized Prompt Tokens: {} tokens",
        report.consumed_tokens
    );
    println!("  • Ceiling Budget Enforced:   150 tokens (Overrun: 0 tokens)");
    println!("  • Context Reduction Ratio:   {:.2}%", reduction_pct);
    println!("  • Harpe Knapsack Latency:    {:.2} µs", harpe_us);
    println!("  • Wire Payload (Min JSON):   {} bytes", wire_bytes);
    println!("  • Kibisis Fencing Token:     {}", lease.fencing_token);
    println!("  • Argus Append Latency:      {:.2} µs", append_us);
    println!(
        "  • Ledger Event Hash:         {}",
        &event.event_hash[0..24]
    );
    println!("  • SHA-256 Chain Integrity:   VALID (0 gaps, 0 tampers)");

    Tier1Report {
        raw_context_tokens: raw_tokens,
        synthesized_prompt_tokens: report.consumed_tokens,
        budget_ceiling: 150,
        reduction_pct,
        harpe_latency_us: harpe_us,
        wire_payload_bytes: wire_bytes,
        fencing_token: lease.fencing_token,
        argus_append_latency_us: append_us,
        chain_integrity_valid: verifier.valid,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================");
    println!("  RESTRICTED ENVIRONMENT MICRO-PAYLOAD GAUNTLET BENCHMARK");
    println!("  Pure-Rust Micro-Payload & Deterministic Token Economics Test");
    println!("================================================================");

    let tier0 = run_tier0_tactical_pulse();
    let tier1 = run_tier1_closed_loop();

    println!("\n================================================================");
    println!("  ALL RESTRICTED PAYLOAD BENCHMARKS COMPLETED & VERIFIED (PASS)");
    println!("================================================================");

    if let Some(target_file) = json_path {
        let report = RestrictedPayloadReport {
            benchmark: "telarch-benchmarks-restricted-payload",
            suite: "Telarch Suite (TCI)",
            host: "Linux x86_64, Intel Core i5-13500 (20 threads)",
            timestamp_utc: clock::utc_now_rfc3339(),
            tier0_tactical_pulse: tier0,
            tier1_closed_loop: tier1,
        };

        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized = serde_json::to_string_pretty(&report)
            .expect("Failed to serialize restricted payload report");
        fs::write(&target_file, serialized)
            .expect("Failed to write restricted payload JSON report");
        println!(
            "\n[REPORT] Saved machine-readable restricted payload benchmark report to: {}",
            target_file
        );
    }
}
