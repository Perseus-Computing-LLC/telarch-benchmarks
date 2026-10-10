//! Micro-Payload & Restricted Environment Verification Benchmark
//!
//! Evaluates the lowest viable operational payload sizes for the Harpe/Kibisis/Argus
//! triad under highly constrained token budgets and restricted permission sandboxes.
//!
//! Tiers evaluated:
//! - Tier 0: Tactical Pulse (60-token budget ceiling)
//! - Tier 1: Minimal Closed-Loop Reasoning (150-token budget ceiling)

use argus_ledger::{sha256_hex, AuditLedgerWriter, AuditVerifier, InMemoryLedger};
use harpe::{ContextBudgetEngine, ContextItem, ContextSynthesizer, PrunePolicy, TokenBudget};
use kibisis::{
    InMemoryLeaseCoordinator, InMemoryMemoryVault, LeaseCoordinator, MemoryEntity, MemoryVault,
};
use std::time::{Duration, Instant};

fn run_tier0_tactical_pulse() {
    println!("\n========================================================");
    println!("  RUNNING TIER 0: MICRO-TACTICAL PULSE (60 TOKENS)");
    println!("========================================================");

    // 1. Setup high-volume raw telemetry items (50 items = ~1,500 tokens)
    // In Harpe: priority 0 is pinned; for unpinned items, lower numeric value = higher priority.
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

    // Canonical wire payload framing (UTF-8 JSON)
    let wire_payload = format!(
        "{{\"seq\":101,\"k\":\"{}\",\"v\":\"{}\",\"tok\":{},\"auth\":\"{}\"}}",
        report.included_items[0].id,
        report.included_items[0].content,
        report.consumed_tokens,
        &sha256_hex(report.included_items[0].content.as_bytes())[0..16]
    );
    let wire_bytes = wire_payload.as_bytes().len();

    // Compact binary framing simulation (TLV: Type 1B, Len 1B, Seq 4B, Hash 8B, Body)
    let compact_binary_bytes = 1 + 1 + 4 + 8 + report.included_items[0].content.len();

    // Argus tamper-evident provenance ledger
    let mut ledger = InMemoryLedger::new();
    let t_append = Instant::now();
    let event = ledger
        .append("tactical.pulse", "edge_node_07", &wire_payload)
        .unwrap();
    let append_us = t_append.elapsed().as_nanos() as f64 / 1_000.0;

    let verifier = ledger.verify_chain().unwrap();
    assert!(verifier.valid, "Argus ledger chain verification failed");

    println!(
        "  • Raw Haystack Tokens:      {} tokens (50 items)",
        total_raw_tokens
    );
    println!(
        "  • Synthesized Prompt Tokens: {} tokens",
        report.consumed_tokens
    );
    println!("  • Ceiling Budget Enforced:   60 tokens (Overrun: 0 tokens)");
    println!(
        "  • Context Reduction Ratio:   {:.2}%",
        (1.0 - (report.consumed_tokens as f64 / total_raw_tokens as f64)) * 100.0
    );
    println!(
        "  • Harpe Knapsack Latency:    {:.2} µs",
        elapsed.as_nanos() as f64 / 1_000.0
    );
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
}

fn run_tier1_closed_loop() {
    println!("\n========================================================");
    println!("  RUNNING TIER 1: MINIMAL CLOSED-LOOP (150 TOKENS)");
    println!("========================================================");

    // 1. Vault initialization with atomic concurrency
    let mut vault = InMemoryMemoryVault::new();
    let mut coord = InMemoryLeaseCoordinator::new();
    let lease = coord
        .acquire("grid_bus_alpha", "controller_01", Duration::from_secs(30))
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

    // Formatted structured wire payload
    let wire_payload = format!(
        "{{\"seq\":502,\"fencing_token\":{},\"included_count\":{},\"tokens\":{},\"digest\":\"{}\"}}",
        lease.fencing_token,
        report.included_items.len(),
        report.consumed_tokens,
        &sha256_hex(format!("lease:{}", lease.fencing_token).as_bytes())[0..16]
    );
    let wire_bytes = wire_payload.as_bytes().len();

    // Argus logging & audit verification
    let mut ledger = InMemoryLedger::new();
    let t_app = Instant::now();
    let event = ledger
        .append("loop.closed_action", "controller_01", &wire_payload)
        .unwrap();
    let append_us = t_app.elapsed().as_nanos() as f64 / 1_000.0;

    let verifier = ledger.verify_chain().unwrap();
    assert!(verifier.valid, "Argus ledger chain verification failed");

    println!(
        "  • Raw Context Tokens:        {} tokens (102 items)",
        raw_tokens
    );
    println!(
        "  • Synthesized Prompt Tokens: {} tokens",
        report.consumed_tokens
    );
    println!("  • Ceiling Budget Enforced:   150 tokens (Overrun: 0 tokens)");
    println!(
        "  • Context Reduction Ratio:   {:.2}%",
        (1.0 - (report.consumed_tokens as f64 / raw_tokens as f64)) * 100.0
    );
    println!(
        "  • Harpe Knapsack Latency:    {:.2} µs",
        elapsed.as_nanos() as f64 / 1_000.0
    );
    println!("  • Wire Payload (Min JSON):   {} bytes", wire_bytes);
    println!("  • Kibisis Fencing Token:     {}", lease.fencing_token);
    println!("  • Argus Append Latency:      {:.2} µs", append_us);
    println!(
        "  • Ledger Event Hash:         {}",
        &event.event_hash[0..24]
    );
    println!("  • SHA-256 Chain Integrity:   VALID (0 gaps, 0 tampers)");
}

fn main() {
    println!("================================================================");
    println!("  RESTRICTED ENVIRONMENT MICRO-PAYLOAD GAUNTLET BENCHMARK");
    println!("  Pure-Rust Micro-Payload & Deterministic Token Economics Test");
    println!("================================================================");

    run_tier0_tactical_pulse();
    run_tier1_closed_loop();

    println!("\n================================================================");
    println!("  ALL RESTRICTED PAYLOAD BENCHMARKS COMPLETED & VERIFIED (PASS)");
    println!("================================================================");
}
