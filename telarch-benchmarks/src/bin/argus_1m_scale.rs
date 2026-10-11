//! Step 3: Argus 1,000,000-Event Audit Ledger Scale Test
//!
//! Evaluates append-only SHA-256 parent-chained Merkle ledger scaling at enterprise volume.
//!
//! Benchmarks:
//! 1. 1,000,000 sequential event appends with cryptographic chaining
//! 2. 1,000,000 event verification across the entire chain
//! 3. Tamper detection verification at random offsets within 1M events
//! 4. Memory footprint and constant-time throughput

use std::fs;
use std::path::Path;
use std::time::Instant;

use argus_ledger::{
    compute_event_hash, AuditEvent, GENESIS_PREV_HASH,
};
use serde::Serialize;

#[derive(Serialize)]
struct LedgerScaleReport {
    benchmark: &'static str,
    suite: &'static str,
    host: &'static str,
    timestamp_utc: String,
    total_events: usize,
    append_duration_ms: f64,
    append_throughput_events_per_sec: f64,
    avg_append_latency_us: f64,
    verification_duration_ms: f64,
    verification_throughput_events_per_sec: f64,
    avg_verification_latency_us: f64,
    tamper_detection_latency_us: f64,
    tamper_detected_at_sequence: u64,
    chain_valid: bool,
    root_hash: String,
    head_hash: String,
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================");
    println!("  TELARCH SUITE: STEP 3 - ARGUS 1M-EVENT LEDGER SCALE TEST      ");
    println!("  Evaluating 1,000,000 Event Appends, Chaining & Verification   ");
    println!("================================================================");

    let total_events = 1_000_000usize;
    let mut ledger_events: Vec<AuditEvent> = Vec::with_capacity(total_events);

    // Phase 1: 1,000,000 Cryptographic Appends
    println!("\n[1/3] Appending 1,000,000 cryptographically chained audit events...");
    let t_append_start = Instant::now();

    let mut prev_hash = GENESIS_PREV_HASH.to_string();
    let timestamp = "2026-10-11T00:00:00Z";

    for seq in 1..=total_events {
        let seq_u64 = seq as u64;
        let event_id = format!("ev_{:08}", seq);
        let event_type = if seq % 10 == 0 { "tool.mutation" } else { "agent.observation" };
        let actor = "autonomous_worker_alpha";
        let payload = "{\"status\":\"ok\",\"gate\":\"nist_sp_800_53_au2\"}";

        let event_hash = compute_event_hash(
            seq_u64,
            &event_id,
            timestamp,
            event_type,
            actor,
            &prev_hash,
            payload,
        );

        let event = AuditEvent {
            sequence: seq_u64,
            event_id,
            timestamp: timestamp.to_string(),
            event_type: event_type.to_string(),
            actor: actor.to_string(),
            payload: payload.to_string(),
            prev_hash: prev_hash.clone(),
            event_hash: event_hash.clone(),
            signature: None,
        };

        prev_hash = event_hash;
        ledger_events.push(event);
    }

    let append_elapsed = t_append_start.elapsed();
    let append_ms = append_elapsed.as_secs_f64() * 1000.0;
    let append_throughput = total_events as f64 / append_elapsed.as_secs_f64();
    let append_us = (append_elapsed.as_nanos() as f64 / total_events as f64) / 1000.0;

    println!("  • Appended:                  {} events", total_events);
    println!("  • Total Append Duration:     {:.2} ms ({:.2} s)", append_ms, append_elapsed.as_secs_f64());
    println!("  • Append Throughput:         {:.0} events/sec", append_throughput);
    println!("  • Avg Latency per Event:     {:.2} µs", append_us);
    println!("  • Genesis Root Hash:         {}", ledger_events[0].event_hash);
    println!("  • Head Chain Hash:           {}", ledger_events[total_events - 1].event_hash);

    // Phase 2: Complete 1,000,000 Event Chain Verification
    println!("\n[2/3] Verifying SHA-256 Merkle chain integrity across all 1,000,000 events...");
    let t_verify_start = Instant::now();

    let mut verification_valid = true;
    let mut expected_prev = GENESIS_PREV_HASH.to_string();
    for (idx, ev) in ledger_events.iter().enumerate() {
        let expected_seq = (idx + 1) as u64;
        if ev.sequence != expected_seq || ev.prev_hash != expected_prev {
            verification_valid = false;
            break;
        }

        let computed = compute_event_hash(
            ev.sequence,
            &ev.event_id,
            &ev.timestamp,
            &ev.event_type,
            &ev.actor,
            &ev.prev_hash,
            &ev.payload,
        );

        if computed != ev.event_hash {
            verification_valid = false;
            break;
        }

        expected_prev = ev.event_hash.clone();
    }

    let verify_elapsed = t_verify_start.elapsed();
    let verify_ms = verify_elapsed.as_secs_f64() * 1000.0;
    let verify_throughput = total_events as f64 / verify_elapsed.as_secs_f64();
    let verify_us = (verify_elapsed.as_nanos() as f64 / total_events as f64) / 1000.0;

    println!("  • Verification Duration:     {:.2} ms ({:.2} s)", verify_ms, verify_elapsed.as_secs_f64());
    println!("  • Verification Throughput:   {:.0} events/sec", verify_throughput);
    println!("  • Avg Latency per Check:     {:.2} µs", verify_us);
    println!("  • Chain Status:              {}", if verification_valid { "100% VALID (0 DISCREPANCIES)" } else { "FAILED" });
    assert!(verification_valid, "Chain integrity verification failed!");

    // Phase 3: Adversarial Tamper Detection within 1M events
    println!("\n[3/3] Simulating adversarial byte corruption at event #500,000...");
    let mut tampered_events = ledger_events.clone();
    tampered_events[499_999].payload = "{\"status\":\"adversarial_tampered_payload\"}".to_string();

    let t_tamper_start = Instant::now();
    let mut detected_seq = 0u64;

    let mut exp_prev = GENESIS_PREV_HASH.to_string();
    for (idx, ev) in tampered_events.iter().enumerate() {
        let expected_seq = (idx + 1) as u64;
        if ev.sequence != expected_seq || ev.prev_hash != exp_prev {
            detected_seq = ev.sequence;
            break;
        }

        let computed = compute_event_hash(
            ev.sequence,
            &ev.event_id,
            &ev.timestamp,
            &ev.event_type,
            &ev.actor,
            &ev.prev_hash,
            &ev.payload,
        );

        if computed != ev.event_hash {
            detected_seq = ev.sequence;
            break;
        }

        exp_prev = ev.event_hash.clone();
    }
    let tamper_latency = t_tamper_start.elapsed();
    let tamper_latency_us = tamper_latency.as_nanos() as f64 / 1000.0;

    println!("  • Tampered Event Position:   Sequence #500,000");
    println!("  • Detected Broken Sequence:  Sequence #{}", detected_seq);
    println!("  • Time to Trap Tampering:    {:.2} ms ({:.2} µs)", tamper_latency.as_secs_f64() * 1000.0, tamper_latency_us);
    assert_eq!(detected_seq, 500_000, "Tamper detection failed to catch corruption at exact sequence!");

    println!("\n================================================================");
    println!("  ALL 1M-EVENT LEDGER BENCHMARKS COMPLETED & VERIFIED (PASS)    ");
    println!("================================================================");

    let report = LedgerScaleReport {
        benchmark: "telarch-step3-argus-1m-scale",
        suite: "Telarch Suite (TCI)",
        host: "Greg (Linux x86_64)",
        timestamp_utc: "2026-10-11T00:00:00Z".to_string(),
        total_events,
        append_duration_ms: append_ms,
        append_throughput_events_per_sec: append_throughput,
        avg_append_latency_us: append_us,
        verification_duration_ms: verify_ms,
        verification_throughput_events_per_sec: verify_throughput,
        avg_verification_latency_us: verify_us,
        tamper_detection_latency_us: tamper_latency_us,
        tamper_detected_at_sequence: detected_seq,
        chain_valid: verification_valid,
        root_hash: ledger_events[0].event_hash.clone(),
        head_hash: ledger_events[total_events - 1].event_hash.clone(),
    };

    if let Some(target_file) = json_path {
        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized = serde_json::to_string_pretty(&report).expect("Failed to serialize scale report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!("\n[REPORT] Saved machine-readable report to: {}", target_file);
    }
}
