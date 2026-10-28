//! Step 3: Argus 1,000,000-Event Audit Ledger Scale Test
//!
//! Drives the public `InMemoryLedger` API at enterprise volume, so every figure includes input
//! validation, locking, timestamping, hashing and chaining.
//!
//! Benchmarks:
//! 1. 1,000,000 sequential `append` calls
//! 2. `verify_chain` across the entire 1,000,000-event chain
//! 3. Tamper detection with the positional verifier: one corrupted payload at #500,000
//! 4. Memory footprint and constant-time throughput

use std::fs;
use std::path::Path;
use std::time::Instant;

use argus_ledger::FileLedger;
use argus_ledger::{
    AuditLedgerReader, AuditLedgerWriter, AuditVerifier, InMemoryLedger, LedgerError,
    VerificationPolicy,
};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;
#[path = "../host.rs"]
mod host;

#[derive(Serialize)]
struct LedgerScaleReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
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
    /// Where the appends above were timed: no disk is involved.
    append_storage: &'static str,
    /// Payload is the same constant JSON object on every event (see the append loop).
    payload_bytes: usize,
    /// Same appends against the on-disk FileLedger, which syncs every event with
    /// `sync_data` and takes an flock around the write. Measured separately because it is a
    /// different number: it is bounded by the disk, not by SHA-256.
    file_ledger: FileLedgerReport,
}

#[derive(Serialize)]
struct FileLedgerReport {
    events: usize,
    path: String,
    /// Reported by the filesystem of the scratch path, so readers know what the fsync cost
    /// was paid against.
    filesystem: String,
    append_duration_ms: f64,
    argus_file_append_eps: f64,
    avg_append_latency_us: f64,
    /// One `sync_data` per append; the ledger does not batch.
    fsync_per_event: bool,
    verification_duration_ms: f64,
    chain_valid: bool,
}

/// Filesystem type of the mount holding `path`, from /proc/mounts (longest matching mount point).
/// "unknown" when /proc/mounts cannot be read; the number is still valid, it just lacks context.
fn filesystem_of(path: &Path) -> String {
    let canonical = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let Ok(mounts) = fs::read_to_string("/proc/mounts") else {
        return "unknown".to_string();
    };
    let mut best: Option<(usize, String)> = None;
    for line in mounts.lines() {
        let mut parts = line.split_whitespace();
        let (Some(_dev), Some(mount), Some(fstype)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        let mount_path = Path::new(mount);
        if canonical.starts_with(mount_path)
            && best.as_ref().is_none_or(|(len, _)| mount.len() > *len)
        {
            best = Some((mount.len(), fstype.to_string()));
        }
    }
    best.map(|(_, fs)| fs)
        .unwrap_or_else(|| "unknown".to_string())
}

/// Append `events` events to a fresh FileLedger under `dir`, then verify its chain. The payload
/// and actor match the in-memory phase so the two rates differ only in storage.
fn run_file_ledger(dir: &Path, events: usize) -> FileLedgerReport {
    fs::create_dir_all(dir).expect("cannot create scratch directory for the FileLedger run");
    let path = dir.join(format!("argus_1m_scale_{}.jsonl", std::process::id()));
    let _ = fs::remove_file(&path);
    let ledger = FileLedger::new(&path).expect("FileLedger::new failed");

    println!(
        "\n[4/4] Appending {} events to the on-disk FileLedger (sync_data per event) at {}...",
        events,
        path.display()
    );
    let t0 = Instant::now();
    for seq in 1..=events {
        let event_type = if seq % 10 == 0 {
            "tool.mutation"
        } else {
            "agent.observation"
        };
        ledger
            .append(event_type, "autonomous_worker_alpha", PAYLOAD)
            .expect("file append rejected");
    }
    let append_elapsed = t0.elapsed();
    let eps = events as f64 / append_elapsed.as_secs_f64();
    let avg_us = (append_elapsed.as_nanos() as f64 / events as f64) / 1000.0;

    let t1 = Instant::now();
    let report = ledger
        .verify_chain()
        .expect("file ledger verification failed");
    let verify_elapsed = t1.elapsed();
    assert!(report.valid, "FileLedger chain did not verify");

    let filesystem = filesystem_of(dir);
    println!(
        "  • Appended:                  {} events in {:.2} s ({:.0} events/sec, {:.1} µs each, fs={})",
        events,
        append_elapsed.as_secs_f64(),
        eps,
        avg_us,
        filesystem
    );
    println!(
        "  • Chain verified from disk:  {} in {:.2} ms",
        report.valid,
        verify_elapsed.as_secs_f64() * 1000.0
    );
    // The file is the artifact of a benchmark, not a ledger anyone depends on.
    let _ = fs::remove_file(&path);

    FileLedgerReport {
        events,
        path: path.display().to_string(),
        filesystem,
        append_duration_ms: append_elapsed.as_secs_f64() * 1000.0,
        argus_file_append_eps: eps,
        avg_append_latency_us: avg_us,
        fsync_per_event: true,
        verification_duration_ms: verify_elapsed.as_secs_f64() * 1000.0,
        chain_valid: report.valid,
    }
}

/// The constant payload every event carries, in both phases. 43 bytes; hashing cost does not
/// grow with it, so the in-memory rate is a hashing-and-bookkeeping rate for small events.
const PAYLOAD: &str = "{\"gate\":\"nist_sp_800_53_au2\",\"status\":\"ok\"}";

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());
    // --file-events N: how many events the on-disk phase appends. Each one costs a sync_data, so the
    // full million took about twenty minutes on this host's btrfs scratch disk; the default is a
    // tenth of that, and the published 1M on-disk figure is a separate single run with
    // --file-events 1000000 (reports/step3_argus_file_ledger_1m.json).
    let file_events = args
        .iter()
        .position(|a| a == "--file-events")
        .and_then(|idx| args.get(idx + 1))
        .map(|n| n.parse::<usize>().expect("--file-events takes an integer"))
        .unwrap_or(100_000);
    // --file-dir PATH: scratch directory for the ledger file. The default is the system temp dir
    // (TMPDIR when set) rather than the repo, so a run never leaves a 300 MB file in the checkout,
    // and rather than a fixed host path: a clone on another machine has no /opt/data and no
    // right to create it, which is how the alpha.9 EC2 run first failed with EACCES.
    let file_dir = args
        .iter()
        .position(|a| a == "--file-dir")
        .and_then(|idx| args.get(idx + 1).cloned())
        .unwrap_or_else(|| {
            std::env::temp_dir()
                .join("telarch-benchmarks")
                .to_string_lossy()
                .into_owned()
        });

    println!("================================================================");
    println!("  TELARCH SUITE: STEP 3 - ARGUS 1M-EVENT LEDGER SCALE TEST      ");
    println!("  Evaluating 1,000,000 Event Appends, Chaining & Verification   ");
    println!("================================================================");

    let total_events = 1_000_000usize;
    let ledger = InMemoryLedger::new();

    // Phase 1: 1,000,000 Cryptographic Appends
    println!("\n[1/4] Appending 1,000,000 cryptographically chained audit events...");
    let t_append_start = Instant::now();

    for seq in 1..=total_events {
        let event_type = if seq % 10 == 0 {
            "tool.mutation"
        } else {
            "agent.observation"
        };
        ledger
            .append(event_type, "autonomous_worker_alpha", PAYLOAD)
            .expect("append rejected");
    }

    let append_elapsed = t_append_start.elapsed();
    let append_ms = append_elapsed.as_secs_f64() * 1000.0;
    let append_throughput = total_events as f64 / append_elapsed.as_secs_f64();
    let append_us = (append_elapsed.as_nanos() as f64 / total_events as f64) / 1000.0;

    println!("  • Appended:                  {} events", total_events);
    println!(
        "  • Total Append Duration:     {:.2} ms ({:.2} s)",
        append_ms,
        append_elapsed.as_secs_f64()
    );
    println!(
        "  • Append Throughput:         {:.0} events/sec",
        append_throughput
    );
    println!("  • Avg Latency per Event:     {:.2} µs", append_us);

    // Phase 2: Complete 1,000,000 Event Chain Verification
    println!(
        "\n[2/4] Verifying SHA-256 linear hash chain integrity across all 1,000,000 events..."
    );
    let t_verify_start = Instant::now();
    let chain = ledger.verify_chain().expect("verification did not run");
    let verify_elapsed = t_verify_start.elapsed();
    let verification_valid = chain.valid;
    println!("  • Genesis Root Hash:         {}", chain.root_hash);
    println!("  • Head Chain Hash:           {}", chain.head_hash);

    let verify_ms = verify_elapsed.as_secs_f64() * 1000.0;
    let verify_throughput = total_events as f64 / verify_elapsed.as_secs_f64();
    let verify_us = (verify_elapsed.as_nanos() as f64 / total_events as f64) / 1000.0;

    println!(
        "  • Verification Duration:     {:.2} ms ({:.2} s)",
        verify_ms,
        verify_elapsed.as_secs_f64()
    );
    println!(
        "  • Verification Throughput:   {:.0} events/sec",
        verify_throughput
    );
    println!("  • Avg Latency per Check:     {:.2} µs", verify_us);
    println!(
        "  • Chain Status:              {}",
        if verification_valid {
            "100% VALID (0 DISCREPANCIES)"
        } else {
            "FAILED"
        }
    );
    assert!(verification_valid, "Chain integrity verification failed!");

    // Phase 3: Adversarial Tamper Detection within 1M events
    println!("\n[3/4] Simulating adversarial byte corruption at event #500,000...");
    let mut tampered_events = ledger.all_events().expect("export failed");
    tampered_events[499_999].payload = "{\"status\":\"adversarial_tampered_payload\"}".to_string();

    let t_tamper_start = Instant::now();
    let tampered = VerificationPolicy::default()
        .verify(&tampered_events)
        .expect("verification did not run");
    let tamper_latency = t_tamper_start.elapsed();
    let tamper_latency_us = tamper_latency.as_nanos() as f64 / 1000.0;
    let detected_seq = tampered.first_broken_sequence.unwrap_or(0);
    assert!(
        matches!(tampered.failure, Some(LedgerError::HashMismatch(_))),
        "tampering must fail closed"
    );

    println!("  • Tampered Event Position:   Sequence #500,000");
    println!("  • Detected Broken Sequence:  Sequence #{}", detected_seq);
    println!(
        "  • Time to Trap Tampering:    {:.2} ms ({:.2} µs)",
        tamper_latency.as_secs_f64() * 1000.0,
        tamper_latency_us
    );
    assert_eq!(
        detected_seq, 500_000,
        "Tamper detection failed to catch corruption at exact sequence!"
    );

    let file_ledger = run_file_ledger(Path::new(&file_dir), file_events);

    println!("\n================================================================");
    println!("  ALL 1M-EVENT LEDGER BENCHMARKS COMPLETED & VERIFIED (PASS)    ");
    println!("================================================================");

    let report = LedgerScaleReport {
        benchmark: "telarch-step3-argus-1m-scale",
        suite: "Telarch Suite (TCI)",
        host: host::describe(),
        timestamp_utc: clock::utc_now_rfc3339(),
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
        root_hash: chain.root_hash.clone(),
        head_hash: chain.head_hash.clone(),
        append_storage: "in-memory (InMemoryLedger), no disk",
        payload_bytes: PAYLOAD.len(),
        file_ledger,
    };

    if let Some(target_file) = json_path {
        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized =
            serde_json::to_string_pretty(&report).expect("Failed to serialize scale report");
        fs::write(&target_file, serialized).expect("Failed to write JSON report");
        println!(
            "\n[REPORT] Saved machine-readable report to: {}",
            target_file
        );
    }
}
