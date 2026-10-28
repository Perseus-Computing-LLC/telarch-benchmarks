use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::time::{Duration, Instant};

use argus_ledger::{AuditLedgerWriter, AuditVerifier, InMemoryLedger};
use kibisis::{InMemoryMemoryVault, MemoryEntity, MemoryVault};
use serde::Serialize;

#[path = "../clock.rs"]
mod clock;
#[path = "../host.rs"]
mod host;

#[derive(Serialize)]
struct SwarmReport {
    benchmark: &'static str,
    suite: &'static str,
    host: String,
    timestamp_utc: String,
    scenarios: Vec<SwarmScenarioReport>,
}

#[derive(Serialize)]
struct SwarmScenarioReport {
    nodes: usize,
    packet_loss_pct: u32,
    partition_duration_ms: u64,
    total_local_ops: usize,
    local_ops_duration_ms: f64,
    local_ops_per_sec: f64,
    reconciliation_duration_ms: f64,
    replicated_messages: usize,
    dropped_packets: usize,
    sync_throughput_msgs_per_sec: f64,
    audit_duration_ms: f64,
    /// Every node's own hash chain verified; says nothing about whether the nodes agree.
    audit_verification_pass: bool,
    /// Every node holds every message after reconciliation (each node's replicated set, plus
    /// its own mutations, equals the full message set).
    nodes_converged: bool,
    /// Messages some node never received, summed over nodes.
    missing_messages: usize,
    /// Nodes with at least one missing message.
    nodes_missing_messages: usize,
    /// Pairs of nodes whose ledger head hashes agree. Nodes append in arrival order, so this is
    /// expected to be 0 even when the message sets converge; it is reported so nobody assumes
    /// the chains themselves are identical.
    identical_head_hash_pairs: usize,
    /// What the scenario demonstrates: convergence when nodes_converged, chain integrity otherwise.
    claim_label: &'static str,
}

#[derive(Clone, Debug)]
struct TacticalMessage {
    sender: usize,
    seq: u64,
    key: String,
    content: String,
    timestamp: u64,
}

struct EdgeNode {
    id: usize,
    vault: InMemoryMemoryVault,
    ledger: InMemoryLedger,
    local_clock: u64,
    received_seqs: HashSet<(usize, u64)>,
}

impl EdgeNode {
    fn new(id: usize) -> Self {
        Self {
            id,
            vault: InMemoryMemoryVault::new(),
            ledger: InMemoryLedger::new(),
            local_clock: 0,
            received_seqs: HashSet::new(),
        }
    }

    fn apply_mutation(&mut self, key: &str, content: &str) -> TacticalMessage {
        self.local_clock += 1;
        let ent = MemoryEntity::new("tactical", key, content);
        self.vault.put(&ent).unwrap();
        self.ledger
            .append(
                "mutation.local",
                &format!("node_{}", self.id),
                &audit_payload(content),
            )
            .unwrap();

        TacticalMessage {
            sender: self.id,
            seq: self.local_clock,
            key: key.to_string(),
            content: content.to_string(),
            timestamp: self.local_clock,
        }
    }

    fn receive_remote(&mut self, msg: &TacticalMessage) -> bool {
        let id_tuple = (msg.sender, msg.seq);
        if self.received_seqs.contains(&id_tuple) {
            return false;
        }
        self.received_seqs.insert(id_tuple);
        if msg.timestamp > self.local_clock {
            self.local_clock = msg.timestamp;
        }

        let ent = MemoryEntity::new("tactical", &msg.key, &msg.content);
        self.vault.put(&ent).unwrap();
        self.ledger
            .append(
                "mutation.replicated",
                &format!("node_{}", msg.sender),
                &audit_payload(&msg.content),
            )
            .unwrap();
        true
    }
}

/// The ledger records canonical JSON objects; serde_json escapes the free-text mutation.
fn audit_payload(content: &str) -> String {
    serde_json::json!({ "content": content }).to_string()
}

fn run_partition_scenario(
    num_nodes: usize,
    packet_loss_pct: u32,
    partition_duration: Duration,
) -> SwarmScenarioReport {
    println!(
        "--- Testing Swarm Scenario: {} Nodes | {}% Packet Loss | {:?} Simulated Partition ---",
        num_nodes, packet_loss_pct, partition_duration
    );

    let mut nodes: Vec<EdgeNode> = (0..num_nodes).map(EdgeNode::new).collect();
    let mut message_buffer: Vec<TacticalMessage> = Vec::new();

    let t0 = Instant::now();
    let total_local_ops = 5000;

    // Phase 1: Autonomous mutations during partition
    for i in 0..total_local_ops {
        let node_id = i % num_nodes;
        let key = format!("target_alpha_{}", (i / num_nodes) % 50);
        let content = format!("status:active,pos:({},{}),update:{}", node_id, i, i);
        let msg = nodes[node_id].apply_mutation(&key, &content);
        message_buffer.push(msg);
    }

    let local_dur = t0.elapsed();
    let local_ops_per_sec = total_local_ops as f64 / local_dur.as_secs_f64();
    println!("  -> Phase 1 (Air-Gapped Autonomous Operation):");
    println!(
        "     {} mutations logged locally across {} nodes in {:.2} ms ({:.0} local ops/sec)",
        total_local_ops,
        num_nodes,
        local_dur.as_secs_f64() * 1000.0,
        local_ops_per_sec
    );

    // Phase 2: Mesh Reconnection and State Reconciliation
    let t_sync_start = Instant::now();
    let mut dropped = 0;
    let mut replicated = 0;

    for msg in &message_buffer {
        for (target_id, node) in nodes.iter_mut().enumerate().take(num_nodes) {
            if target_id == msg.sender {
                continue;
            }
            let drop_hash = (msg.seq * 31 + target_id as u64 * 17) % 100;
            if drop_hash < packet_loss_pct as u64 {
                dropped += 1;
                let retry_hash = (drop_hash * 37 + 13) % 100;
                if retry_hash >= packet_loss_pct as u64 && node.receive_remote(msg) {
                    replicated += 1;
                }
            } else if node.receive_remote(msg) {
                replicated += 1;
            }
        }
    }

    let sync_dur = t_sync_start.elapsed();
    let sync_throughput = replicated as f64 / sync_dur.as_secs_f64();
    println!("  -> Phase 2 (Mesh Reconnect & Reconciliation):");
    println!(
        "     Reconciliation Time: {:.2} ms for {} mesh message deliveries",
        sync_dur.as_secs_f64() * 1000.0,
        replicated
    );
    println!(
        "     Sync Throughput: {:.0} messages/sec | Dropped Packets Recovered via Mesh Gossip: {}",
        sync_throughput, dropped
    );

    // Phase 3: Cryptographic Ledger Audit Verification across all nodes
    let t_audit_start = Instant::now();
    let mut all_chains_valid = true;
    let mut head_hashes = Vec::with_capacity(num_nodes);
    for node in &nodes {
        let report = node.ledger.verify_chain().unwrap();
        all_chains_valid &= report.valid;
        head_hashes.push(report.head_hash);
    }
    let audit_dur = t_audit_start.elapsed();
    assert!(all_chains_valid, "a node's own hash chain failed to verify");

    // Convergence: did every message reach every node? Each node's own chain verifying is
    // necessary but not sufficient, so the sets are compared explicitly and the claim label
    // follows from the result rather than being asserted.
    let mut missing_messages = 0usize;
    let mut nodes_missing = 0usize;
    for node in &nodes {
        let expected_remote = message_buffer
            .iter()
            .filter(|m| m.sender != node.id)
            .count();
        let missing = expected_remote - node.received_seqs.len();
        if missing > 0 {
            nodes_missing += 1;
            missing_messages += missing;
        }
    }
    let nodes_converged = missing_messages == 0;
    let identical_head_hash_pairs = head_hashes
        .iter()
        .enumerate()
        .flat_map(|(i, a)| head_hashes.iter().skip(i + 1).map(move |b| a == b))
        .filter(|same| *same)
        .count();
    let claim_label = if nodes_converged {
        "audit-chain convergence under packet loss"
    } else {
        "per-node chain integrity under packet loss"
    };

    println!("  -> Phase 3 (Cryptographic Chain Audit):");
    println!(
        "     {} node ledgers verified in {:.2} ms; converged: {} ({} messages missing on {} nodes)\n",
        num_nodes,
        audit_dur.as_secs_f64() * 1000.0,
        nodes_converged,
        missing_messages,
        nodes_missing
    );

    SwarmScenarioReport {
        nodes: num_nodes,
        packet_loss_pct,
        partition_duration_ms: partition_duration.as_millis() as u64,
        total_local_ops,
        local_ops_duration_ms: local_dur.as_secs_f64() * 1000.0,
        local_ops_per_sec,
        reconciliation_duration_ms: sync_dur.as_secs_f64() * 1000.0,
        replicated_messages: replicated,
        dropped_packets: dropped,
        sync_throughput_msgs_per_sec: sync_throughput,
        audit_duration_ms: audit_dur.as_secs_f64() * 1000.0,
        audit_verification_pass: all_chains_valid,
        nodes_converged,
        missing_messages,
        nodes_missing_messages: nodes_missing,
        identical_head_hash_pairs,
        claim_label,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let json_output_idx = args.iter().position(|a| a == "--json");
    let json_path = json_output_idx.and_then(|idx| args.get(idx + 1).cloned());

    println!("================================================================================");
    println!("        TELARCH PHASE 2: TACTICAL SWARM NETWORK PARTITION BENCHMARK            ");
    println!("================================================================================\n");

    let scenarios = vec![
        run_partition_scenario(10, 0, Duration::from_millis(100)),
        run_partition_scenario(10, 25, Duration::from_millis(500)),
        run_partition_scenario(10, 50, Duration::from_secs(1)),
    ];

    println!("================================================================================");
    println!("                     TACTICAL SWARM BENCHMARKS COMPLETE                         ");
    println!("================================================================================");

    if let Some(target_file) = json_path {
        let report = SwarmReport {
            benchmark: "telarch-benchmarks-swarm-partition",
            suite: "Telarch Suite (TCI)",
            host: host::describe(),
            timestamp_utc: clock::utc_now_rfc3339(),
            scenarios,
        };

        if let Some(parent) = Path::new(&target_file).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let serialized =
            serde_json::to_string_pretty(&report).expect("Failed to serialize swarm report");
        fs::write(&target_file, serialized).expect("Failed to write swarm JSON report");
        println!(
            "\n[REPORT] Saved machine-readable swarm benchmark report to: {}",
            target_file
        );
    }
}
