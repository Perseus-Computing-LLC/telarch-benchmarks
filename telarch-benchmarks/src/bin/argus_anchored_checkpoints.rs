//! Gap 6: Argus Anchored Heads & Signed Checkpoints Benchmark
//!
//! Evaluates Argus ledger against the full-chain rewrite vulnerability:
//! 1. Appends 10,000 cryptographically chained events.
//! 2. Signs an Ed25519 checkpoint in C2SP signed-note style.
//! 3. Proves that an attacker who alters event 5,000 and rewrites all subsequent hashes
//!    fools unanchored verification, but is deterministically rejected when verified
//!    against an anchored head checkpoint.
//! 4. Verifies that forged checkpoints signed with unauthorized keys are rejected.

use std::fs;
use std::path::Path;
use std::time::Instant;

use argus_ledger::{
    compute_event_hash, AuditEvent, AuditLedgerReader, AuditLedgerWriter, AuditVerifier,
    ChainAnchor, InMemoryLedger, VerificationPolicy, GENESIS_PREV_HASH,
};
use ed25519_dalek::{Signer, SigningKey, Verifier, VerifyingKey};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;
#[path = "../host.rs"]
mod host;

const CHECKPOINT_ORIGIN: &str = "telarch.ai/argus/checkpoint/v1";

fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(s, "{:02x}", b);
    }
    s
}

#[derive(Serialize)]
struct CheckpointReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
    timestamp_utc: String,
    total_events: usize,
    checkpoint_origin: &'static str,
    checkpoint_sequence: u64,
    checkpoint_head_hash: String,
    checkpoint_signature_hex: String,
    checkpoint_signing_latency_us: f64,
    honest_chain_verified_anchored: bool,
    unanchored_rewrite_evaded_detection: bool,
    anchored_rewrite_caught: bool,
    tamper_detection_error_detail: String,
    forged_checkpoint_signature_rejected: bool,
    anchored_verification_duration_ms: f64,
    anchored_verification_throughput_events_per_sec: f64,
}

fn format_checkpoint_note(origin: &str, sequence: u64, head_hash: &str) -> String {
    format!("{origin}\n{sequence}\n{head_hash}\n")
}

fn relink_chain(events: &mut [AuditEvent]) {
    let mut prev = GENESIS_PREV_HASH.to_string();
    for ev in events.iter_mut() {
        ev.prev_hash = prev;
        let new_hash = compute_event_hash(
            ev.sequence,
            &ev.event_id,
            &ev.timestamp,
            &ev.event_type,
            &ev.actor,
            &ev.prev_hash,
            &ev.payload,
        );
        ev.event_hash = new_hash.clone();
        prev = new_hash;
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================================");
    println!("     TELARCH GAP 6: ARGUS ANCHORED HEADS & SIGNED CHECKPOINTS BENCHMARK        ");
    println!("          {}", host::describe());
    println!("================================================================================\n");

    // 1. Build an honest ledger with 10,000 events
    let total_events = 10_000;
    println!("[1/4] Appending {total_events} events into honest Argus ledger...");
    let ledger = InMemoryLedger::new();
    for i in 1..=total_events {
        let payload = format!(r#"{{"action":"transact","index":{i},"value":100}}"#);
        ledger
            .append("tx.commit", "agent_executor", &payload)
            .unwrap();
    }
    let honest_events = ledger.all_events().unwrap();
    let honest_head = honest_events.last().unwrap();
    let honest_head_seq = honest_head.sequence;
    let honest_head_hash = honest_head.event_hash.clone();

    // 2. Sign a checkpoint note using authoritative Ed25519 keypair
    println!("[2/4] Signing Ed25519 checkpoint note for head sequence {honest_head_seq}...");
    let auth_seed = [42u8; 32];
    let auth_signing_key = SigningKey::from_bytes(&auth_seed);
    let auth_verifying_key: VerifyingKey = auth_signing_key.verifying_key();

    let checkpoint_text =
        format_checkpoint_note(CHECKPOINT_ORIGIN, honest_head_seq, &honest_head_hash);
    let t_sign_start = Instant::now();
    let checkpoint_signature = auth_signing_key.sign(checkpoint_text.as_bytes());
    let sign_latency_us = t_sign_start.elapsed().as_nanos() as f64 / 1_000.0;
    let signature_hex = to_hex(&checkpoint_signature.to_bytes());

    // Verify signature immediately
    auth_verifying_key
        .verify(checkpoint_text.as_bytes(), &checkpoint_signature)
        .expect("Self-signature verification must pass");
    println!("  • Checkpoint Head: {honest_head_hash}");
    println!("  • Signing Latency: {sign_latency_us:.2} µs");

    // 3. Verify honest chain with anchor
    println!("[3/4] Verifying honest chain against anchored checkpoint...");
    let anchor = ChainAnchor {
        sequence: honest_head_seq,
        event_hash: honest_head_hash.clone(),
    };
    let anchored_policy = VerificationPolicy {
        anchor: Some(anchor),
        ..Default::default()
    };

    let t_verify_start = Instant::now();
    let honest_verify = ledger.verify_chain_with(&anchored_policy).unwrap();
    let verify_elapsed = t_verify_start.elapsed();
    let verify_ms = verify_elapsed.as_secs_f64() * 1000.0;
    let verify_throughput = total_events as f64 / verify_elapsed.as_secs_f64();
    assert!(
        honest_verify.valid,
        "Honest chain must verify against honest anchor"
    );
    println!("  • Honest Chain Verification: VALID in {verify_ms:.2} ms ({verify_throughput:.0} events/sec)");

    // 4. Adversarial scenario: full rewrite starting at sequence 5,000
    println!("[4/4] Executing adversarial full-chain rewrite starting at sequence 5,000...");
    let mut rewritten_events = honest_events.clone();
    let tampered_seq = 5_000;
    rewritten_events[tampered_seq - 1].payload =
        r#"{"action":"transact","index":5000,"value":999999999}"#.to_string();
    relink_chain(&mut rewritten_events);

    let rewritten_head_hash = rewritten_events.last().unwrap().event_hash.clone();
    println!("  • Honest Head Hash:    {honest_head_hash}");
    println!("  • Rewritten Head Hash: {rewritten_head_hash}");
    assert_ne!(
        honest_head_hash, rewritten_head_hash,
        "Head hashes must diverge"
    );

    // Check unanchored verification: does it get fooled?
    let unanchored_verify = VerificationPolicy::default()
        .verify(&rewritten_events)
        .unwrap();
    assert!(
        unanchored_verify.valid,
        "Unanchored verification on a fully relinked chain is mathematically self-consistent"
    );
    println!("  • Unanchored Verifier Result: VALID (Tamper evaded detection without anchor)");

    // Check anchored verification: does it catch the rewrite?
    let anchored_check = anchored_policy.verify(&rewritten_events);
    let (anchored_caught, err_detail) = match anchored_check {
        Ok(report) => {
            if !report.valid {
                (
                    true,
                    format!(
                        "Failed validation: broken_seq={:?}",
                        report.first_broken_sequence
                    ),
                )
            } else {
                (false, "Evaded anchor (UNEXPECTED)".to_string())
            }
        }
        Err(e) => (true, format!("{e:?}")),
    };
    assert!(
        anchored_caught,
        "Anchored verification MUST catch rewritten chain"
    );
    println!("  • Anchored Verifier Result: REJECTED (Tamper caught fail-closed)");
    println!("  • Reason: {err_detail}");

    // Test forged checkpoint signature rejection
    let attacker_seed = [99u8; 32];
    let attacker_signing_key = SigningKey::from_bytes(&attacker_seed);
    let forged_checkpoint_text =
        format_checkpoint_note(CHECKPOINT_ORIGIN, honest_head_seq, &rewritten_head_hash);
    let forged_signature = attacker_signing_key.sign(forged_checkpoint_text.as_bytes());
    let forged_rejected = auth_verifying_key
        .verify(forged_checkpoint_text.as_bytes(), &forged_signature)
        .is_err();
    assert!(
        forged_rejected,
        "Forged checkpoint signature must be rejected"
    );
    println!("  • Forged Checkpoint Signature: REJECTED by authoritative public key");

    println!("\n================================================================================");
    println!("                GAP 6 ANCHORED HEADS BENCHMARK COMPLETE (PASS)                  ");
    println!("================================================================================");

    if let Some(target_file) = json_path {
        let rep = CheckpointReport {
            benchmark: "telarch-gap6-argus-anchored-heads",
            suite: "Telarch Suite (TCI)",
            host: host::describe(),
            timestamp_utc: clock::utc_now_rfc3339(),
            total_events,
            checkpoint_origin: CHECKPOINT_ORIGIN,
            checkpoint_sequence: honest_head_seq,
            checkpoint_head_hash: honest_head_hash,
            checkpoint_signature_hex: signature_hex,
            checkpoint_signing_latency_us: sign_latency_us,
            honest_chain_verified_anchored: honest_verify.valid,
            unanchored_rewrite_evaded_detection: unanchored_verify.valid,
            anchored_rewrite_caught: anchored_caught,
            tamper_detection_error_detail: err_detail,
            forged_checkpoint_signature_rejected: forged_rejected,
            anchored_verification_duration_ms: verify_ms,
            anchored_verification_throughput_events_per_sec: verify_throughput,
        };

        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized = serde_json::to_string_pretty(&rep).unwrap();
        fs::write(&target_file, serialized).expect("Writing json report failed");
        println!("\n[REPORT] Saved machine-readable report to: {target_file}");
    }
}
