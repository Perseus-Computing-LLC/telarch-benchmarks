#!/usr/bin/env python3
"""Builds claims.json from the reports that `make bench` writes to reports/.

Every claim value is read from a report, so claims.json always matches the reports committed beside
it. One `make bench` run gives one value per claim: run_count is 1, and median and range are null.

When repeated runs exist under reports/runs/<date>/<run>/ (scripts/run_once.sh writes them), the
latest date's runs are read too: each numeric claim's value becomes the median across those runs,
range becomes [min, max] and run_count the number of runs. Text in a claim (statement, scope) comes
from reports/, which holds the last run. TELARCH_RUNS_DATE selects another date.

Before writing, the script checks that:
1. the output has the fields claims_schema.json requires (the whole file is validated against the
   schema when the jsonschema package is installed);
2. the output contains no email address and not this machine's hostname.
"""

import json
import os
import re
import statistics
import tomllib

import bench_env

ROOT = bench_env.ROOT
REPO_URL = "https://github.com/Perseus-Computing-LLC/telarch-benchmarks"
FLAGS = "--release (opt-level=3, codegen-units=16)"  # Cargo's release-profile defaults; Cargo.toml keeps them
NETWORK_RE = re.compile(r"std::net|TcpStream|TcpListener|UdpSocket|std::process::Command|reqwest|hyper::|ureq::")


def load_report(report_dir: str, filename: str) -> dict:
    p = os.path.join(report_dir, filename)
    if not os.path.exists(p):
        raise FileNotFoundError(f"Missing benchmark report: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def direct_dependencies() -> dict:
    """Direct [dependencies], target-specific ones included, of each vendored crate."""
    out = {}
    for d in bench_env.VENDORED:
        with open(os.path.join(ROOT, d, "Cargo.toml"), "rb") as f:
            t = tomllib.load(f)
        deps = set(t.get("dependencies", {}))
        for section in t.get("target", {}).values():
            deps |= set(section.get("dependencies", {}))
        out[d] = sorted(deps)
    return out


def network_scan() -> tuple:
    """Regex search of the vendored crate sources for socket, HTTP and process-spawn APIs."""
    files, hits = 0, []
    for d in bench_env.VENDORED:
        for dirpath, _, names in sorted(os.walk(os.path.join(ROOT, d, "src"))):
            for n in sorted(names):
                if not n.endswith(".rs"):
                    continue
                files += 1
                p = os.path.join(dirpath, n)
                with open(p, encoding="utf-8") as f:
                    hits += [f"{os.path.relpath(p, ROOT)}:{i}" for i, line in enumerate(f, 1) if NETWORK_RE.search(line)]
    return files, hits


def check(output: dict) -> None:
    with open(os.path.join(ROOT, "claims_schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema:
        jsonschema.validate(output, schema)
    else:
        props = schema["properties"]
        groups = [("claims.json", output, schema["required"]), ("meta", output["meta"], props["meta"]["required"])]
        groups += [(f"claim {k}", v, props["claims"]["additionalProperties"]["required"]) for k, v in output["claims"].items()]
        groups += [(f"fact {k}", v, props["facts"]["additionalProperties"]["required"]) for k, v in output["facts"].items()]
        groups += [(f"retired entry {i}", v, props["retired"]["items"]["required"]) for i, v in enumerate(output["retired"])]
        for name, obj, required in groups:
            missing = [k for k in required if k not in obj]
            if missing:
                raise AssertionError(f"{name} lacks {', '.join(missing)}")
    text = json.dumps(output, ensure_ascii=False)
    if bench_env.has_hostname(text):
        raise AssertionError("claims.json would contain this machine's hostname")
    m = bench_env.EMAIL_RE.search(text)
    if m:
        raise AssertionError(f"claims.json would contain an email address: {m.group(0)}")


def build(report_dir: str) -> dict:
    """claims.json content from the reports in one directory: one value per claim, run_count 1."""
    p1 = load_report(report_dir, "microbenchmarks_phase1.json")
    curve = load_report(report_dir, "step1_knapsack_cost_curve.json")
    adv = load_report(report_dir, "step2_adversarial_contention.json")
    arg = load_report(report_dir, "step3_argus_1m_scale.json")
    gap1 = load_report(report_dir, "gap1_harpe_quality.json")
    gap3 = load_report(report_dir, "gap3_kibisis_multiproc.json")
    gap6 = load_report(report_dir, "gap6_argus_anchored_checkpoints.json")
    swarm = load_report(report_dir, "swarm_partition.json")

    commit = bench_env.commit()
    method_ref = "main" if commit == "unknown" or commit.endswith("-dirty") else bench_env.commit(full=True)
    crates = bench_env.crate_versions()
    toolchain = bench_env.toolchain()
    hardware = f"{bench_env.hardware()}; {bench_env.pinning(os.environ.get('TELARCH_PIN', ''))}"

    claims = {}

    def add_claim(cid, statement, val, unit, scope, dataset, raw_path, method, limits):
        claims[cid] = {
            "id": cid,
            "statement": statement,
            "value": val,
            "unit": unit,
            "scope": scope,
            "dataset": dataset,
            "hardware": hardware,
            "toolchain": toolchain,
            "commit": commit,
            "crate_versions": crates,
            "run_count": 1,
            "median": None,
            "range": None,
            "raw_data_path": raw_path,
            "method_link": f"{REPO_URL}/blob/{method_ref}/{method}",
            "limits": limits,
            "status": "measured",
            "note": "",
        }

    # 1. Harpe microbenchmarks
    small, large = p1["harpe"]["small_context"], p1["harpe"]["large_context"]
    add_claim(
        "harpe_small_p50_us",
        f"Harpe selects and packs {small['items']} context items within budget at microsecond median latency",
        round(small["latency_p50_us"], 3),
        "µs",
        f"{small['items']} context items, {small['budget_tokens']:,}-token ceiling, single process, median of {small['iterations']:,} calls",
        "Synthetic context items generated by the program",
        "reports/microbenchmarks_phase1.json",
        "telarch-benchmarks/src/main.rs",
        "In-memory, single-threaded, synthetic context items",
    )
    add_claim(
        "harpe_large_p50_us",
        f"Harpe selects and packs {large['items']} context items within a {large['budget_tokens']:,}-token budget: median latency",
        round(large["latency_p50_us"], 3),
        "µs",
        f"{large['items']} context items, {large['budget_tokens']:,}-token ceiling, single process, median of {large['iterations']:,} calls",
        "Synthetic context items generated by the program",
        "reports/microbenchmarks_phase1.json",
        "telarch-benchmarks/src/main.rs",
        "In-memory, single-threaded, synthetic context items",
    )
    add_claim(
        "harpe_large_reduction_pct",
        f"Context reduction ratio for {large['items']}-item scenario enforcing a {large['budget_tokens']:,}-token ceiling",
        round(p1["harpe"]["token_optimization_pct"], 2),
        "%",
        f"{large['items']} context items, {large['budget_tokens']:,}-token ceiling",
        "Synthetic context items generated by the program",
        "reports/microbenchmarks_phase1.json",
        "telarch-benchmarks/src/main.rs",
        "Fixed synthetic haystack; reduction depends on input token distribution",
    )

    # 2. Harpe 100k-turn cost curve (the 1,200-token ceiling is a constant in knapsack_cost_curve.rs)
    turns = curve["total_turns_simulated"]
    curve_src = ("reports/step1_knapsack_cost_curve.json", "telarch-benchmarks/src/bin/knapsack_cost_curve.rs")
    add_claim(
        "harpe_curve_turns",
        "Simulated autonomous agent invocation turns executed under continuous budget enforcement",
        turns,
        "turns",
        f"{turns:,} simulated agent turns, 1,200-token ceiling",
        "Synthetic multi-turn agent trajectory with pinned system items and variable context items",
        *curve_src,
        "Simulated agent context trajectory, in-memory heuristic token estimation",
    )
    add_claim(
        "harpe_curve_reduction_pct",
        f"Tokens removed over {turns:,} simulated turns relative to unbudgeted accumulation",
        round(curve["token_reduction_ratio_pct"], 2),
        "%",
        f"{turns:,} synthetic turns, 1,200-token ceiling",
        f"Synthetic agent trajectory ({curve['unbudgeted_total_tokens'] / 1e6:.1f}M raw tokens vs "
        f"{curve['harpe_total_tokens'] / 1e6:.1f}M synthesized tokens)",
        *curve_src,
        "Baseline is unbudgeted accumulation of the same items; financial savings depend on model pricing. "
        + curve["reduction_note"],
    )
    add_claim(
        "harpe_curve_overruns",
        "Count of simulated agent turns that exceeded the designated 1,200-token ceiling",
        curve["budget_overruns"],
        "turns",
        f"{turns:,} simulated turns, 1,200-token ceiling",
        "Synthetic agent trajectory",
        *curve_src,
        "Strict knapsack invariant holds assuming token counter matches downstream tokenizer",
    )
    add_claim(
        "harpe_curve_avg_us",
        f"Average Harpe synthesize latency per turn over {turns:,} turns (the synthesize call only)",
        round(curve["synthesize_us"], 2),
        "µs",
        f"{turns:,} synthetic turns, 1,200-token ceiling, 34 items per turn",
        "Synthetic agent trajectory",
        *curve_src,
        "Single process; CPU pinning as stated under hardware. Building the turn and counting the baseline "
        "are timed separately as harpe_curve_harness_us",
    )
    add_claim(
        "harpe_curve_harness_us",
        f"Harness overhead per turn over {turns:,} turns: building the 34 synthetic items and counting the "
        "unbudgeted baseline, not Harpe",
        round(curve["harness_overhead_us"], 2),
        "µs",
        f"{turns:,} synthetic turns",
        "Synthetic agent trajectory",
        *curve_src,
        "Published so the per-turn figure above cannot be read as including this work",
    )

    # 3. Kibisis in-process leases. Turnover in these scenarios is by explicit release; the requested
    # TTL (a constant in adversarial_contention.rs) is one the coordinator honours as given.
    s16, s32 = adv["scenario_16_workers"], adv["scenario_32_workers"]
    adv_src = ("reports/step2_adversarial_contention.json", "telarch-benchmarks/src/bin/adversarial_contention.rs")
    for cid, sc in (("kib_lease16_cycles", s16), ("kib_lease32_cycles", s32)):
        add_claim(
            cid,
            f"Successful lease cycles/s (acquire, work, release) with {sc['worker_count']} competing workers",
            round(sc["lease_cycles_per_sec"]),
            "cycles/s",
            f"{sc['worker_count']} worker threads, one contended resource, in-memory Rust coordinator",
            f"{sc['total_attempts']:,} acquire attempts, {sc['successful_leases']:,} successful; "
            f"{sc['lease_ttl_requested_ms']} ms requested lease TTL",
            *adv_src,
            "Rejected attempts are not counted. The holder's critical section is a thread yield, so the rate "
            "is bounded by hand-over cost, not by work done under the lease",
        )
    for cid, sc in (("kib_lease16_decisions", s16), ("kib_lease32_decisions", s32)):
        add_claim(
            cid,
            f"Lease decisions/s under contention (in-memory Rust coordinator), {sc['worker_count']} competing workers",
            round(sc["lease_decisions_per_sec"]),
            "decisions/s",
            f"{sc['worker_count']} worker threads, one contended resource, in-memory Rust coordinator",
            f"{sc['total_attempts']:,} acquire attempts; rejection rate {sc['rejection_rate'] * 100:.1f}%",
            *adv_src,
            "Counts every answered acquire call, rejections included; a rejection is a map probe under a lock, "
            "not a lease operation. See the matching cycles/s claim for the lease rate",
        )
    expiry = adv["lease_expiry_turnover"]
    add_claim(
        "kib_lease_expiry_recovery_ms",
        "Time until an abandoned lease (never released) could be acquired by another holder",
        round(expiry["recovery_ms"], 1),
        "ms",
        f"One abandoned holder, one polling successor, {expiry['lease_ttl_requested_ms']} ms requested TTL",
        "In-memory Rust coordinator",
        *adv_src,
        "The coordinator keeps deadlines in whole seconds and rounds up, so recovery takes between the TTL and "
        "the TTL plus one second",
    )
    attempts = s16["total_attempts"] + s32["total_attempts"]
    add_claim(
        "kib_split_brain",
        "Moments two competing workers held the write lease simultaneously",
        s16["split_brain_violations"] + s32["split_brain_violations"],
        "events",
        f"{attempts:,} lease contention attempts across {s16['worker_count']} and {s32['worker_count']} workers",
        "Multi-threaded adversarial contention cycles",
        *adv_src,
        "Holds for single-process coordinator; multi-process requires SQLite coordinator",
    )
    add_claim(
        "kib_rollbacks",
        "Fencing token regressions observed under adversarial thread contention",
        s16["fencing_token_rollbacks"] + s32["fencing_token_rollbacks"],
        "events",
        f"{attempts:,} contention cycles",
        "Multi-threaded adversarial contention cycles",
        *adv_src,
        "Fencing token counter is an atomic integer in RAM",
    )
    add_claim(
        "kib_put_ops",
        "Sequential vault write ingestion rate with audit chaining",
        round(p1["kibisis"]["vault_put_ops_per_sec"]),
        "ops/s",
        f"{p1['kibisis']['vault_put_entities']:,} in-memory writes, each hashed into vault audit journal",
        "Synthetic key-value memory entities",
        "reports/microbenchmarks_phase1.json",
        "telarch-benchmarks/src/main.rs",
        "In-memory vault, no disk I/O, no encryption in this Rust benchmark",
    )

    lease8 = p1["kibisis"]["concurrency_leases"]
    add_claim(
        "kib_lease8_round_trips",
        f"Contended lease round-trip: completed acquire, renew, release cycles/s with {lease8['threads']} threads "
        f"on {lease8['resources']} resources",
        round(lease8["round_trips_per_sec"]),
        "round trips/s",
        f"{lease8['threads']} threads, {lease8['resources']} shared resources, in-memory Rust coordinator; "
        f"{lease8['acquisitions']:,} of {lease8['total_attempts']:,} attempts completed, "
        f"{lease8['conflict_rejections']:,} rejected while another thread held the lease",
        "Synthetic resource names, no work under the lease",
        "reports/microbenchmarks_phase1.json",
        "telarch-benchmarks/src/main.rs",
        "The coordinator lock is released between calls so rejections can happen; rejected attempts are not "
        "counted as round trips",
    )

    # 4. Argus ledger (the edited event and the payload are constants in argus_1m_scale.rs)
    n = arg["total_events"]
    tamper_seq = arg["tamper_detected_at_sequence"]
    tamper_ms = round(arg["tamper_detection_latency_us"] / 1000, 2)
    arg_src = ("reports/step3_argus_1m_scale.json", "telarch-benchmarks/src/bin/argus_1m_scale.rs")
    add_claim(
        "argus_events",
        "Total events committed in the million-event scale test",
        n,
        "events",
        f"{n:,} sequential events",
        "Synthetic structured agent tool execution records",
        *arg_src,
        "In-memory linear ledger, no disk persistence in Rust crate",
    )
    payload_bytes = arg["payload_bytes"]
    add_claim(
        "argus_append_eps",
        "In-memory audit event append throughput through the public API with SHA-256 chaining (no disk)",
        round(arg["append_throughput_events_per_sec"]),
        "events/s",
        f"{n:,} events through the public API, each SHA-256 chained, InMemoryLedger",
        f"The same {payload_bytes}-byte JSON payload on every event",
        *arg_src,
        "In-memory ledger with a constant small payload: no fsync, no disk write, no payload variation. "
        "argus_file_append_eps is the on-disk rate",
    )
    add_claim(
        "argus_append_us",
        "Average in-memory append latency per event with streaming SHA-256 computation (no disk)",
        round(arg["avg_append_latency_us"], 3),
        "µs",
        f"{n:,} events, InMemoryLedger",
        f"The same {payload_bytes}-byte JSON payload on every event",
        *arg_src,
        "In-memory ledger, constant payload",
    )
    fl = arg["file_ledger"]
    add_claim(
        "argus_file_append_eps",
        f"Argus on-disk FileLedger append throughput over {fl['events']:,} events, one sync_data per event",
        round(fl["argus_file_append_eps"]),
        "events/s",
        f"{fl['events']:,} sequential appends to a JSON-L file on {fl['filesystem']} under the host's scratch "
        f"directory, flock and sync_data on every append; {payload_bytes}-byte constant payload",
        "Synthetic events, constant payload",
        *arg_src,
        "Bounded by the disk's sync latency, not by hashing; another filesystem or device gives another number. "
        "This is the durable rate; argus_append_eps involves no disk",
    )
    # The full million on disk took 1,161 s on this host, so it was run once and kept as its own report
    # rather than repeated. Its claim says run_count 1; aggregate() leaves it alone. The run's own
    # run_env.txt (reports/runs/<date>/file_ledger_1m/) says which commit and crate versions produced it:
    # when they are not the current ones, the figure was carried over from an earlier release and the
    # claim says so instead of repeating the current versions.
    one_m_path = os.path.join(ROOT, "reports", "step3_argus_file_ledger_1m.json")
    if os.path.exists(one_m_path):
        with open(one_m_path, encoding="utf-8") as f:
            one_m = json.load(f)["file_ledger"]
        add_claim(
            "argus_file_append_1m_eps",
            f"Argus on-disk FileLedger append throughput over {one_m['events']:,} events, one sync_data per event, "
            "single run",
            round(one_m["argus_file_append_eps"]),
            "events/s",
            f"{one_m['events']:,} sequential appends to a JSON-L file on {one_m['filesystem']} under the host's "
            f"scratch directory, flock and sync_data on every append; {payload_bytes}-byte constant payload; "
            f"{one_m['append_duration_ms'] / 1000:.0f} s wall",
            "Synthetic events, constant payload",
            "reports/step3_argus_file_ledger_1m.json",
            "telarch-benchmarks/src/bin/argus_1m_scale.rs",
            "One run, not a median: at this rate the phase takes about twenty minutes, so the repeated runs append "
            f"fewer events (argus_file_append_eps). The same {one_m['events']:,} events in memory are argus_append_eps",
        )
        claim = claims["argus_file_append_1m_eps"]
        claim["note"] = "Single run; see limits."
        prov = one_m_provenance()
        if prov:
            if prov.get("commit"):
                claim["commit"] = prov["commit"]
            if prov.get("pin"):
                claim["hardware"] = f"{bench_env.hardware()}; {bench_env.pinning(prov['pin'])}"
            if prov.get("crate_versions"):
                claim["crate_versions"] = prov["crate_versions"]
                # argus_1m_scale exercises argus-ledger only, so that is the version the statement names.
                stale = [c for c in ("argus-ledger",) if prov["crate_versions"].get(c) != crates[c]]
                if stale:
                    was = ", ".join(f"{c} {prov['crate_versions'][c]}" for c in stale)
                    now = ", ".join(crates[c] for c in stale)
                    claim["carried_over"] = {"run": prov["run"], "commit": prov.get("commit"), "crate_versions": prov["crate_versions"]}
                    claim["statement"] += f", measured against {was} and not repeated for {now}"
                    claim["limits"] += (
                        f". Carried over: measured at commit {prov.get('commit')} against {was}, not repeated for "
                        f"{now}; argus_file_append_eps is the re-measured on-disk rate"
                    )
                    claim["note"] = f"Single run, carried over from {prov['run']}; see limits."

    add_claim(
        "argus_verify_eps",
        f"Cryptographic verification throughput verifying {n:,} events",
        round(arg["verification_throughput_events_per_sec"]),
        "events/s",
        f"Full verification walk of {n:,} events from genesis to head",
        f"{n:,} cryptographically chained event records",
        *arg_src,
        "Linear O(n) traversal in memory",
    )
    add_claim(
        "argus_tamper_seq",
        "Sequence number reported for an edited event when the ledger is verified from the first event",
        tamper_seq,
        "sequence no.",
        "Payload of event 500,000 replaced; verified from genesis",
        f"Corrupted {n:,} event ledger",
        *arg_src,
        "Linear walk from event 1 stops fail-closed at first broken link",
    )
    add_claim(
        "argus_tamper_ms",
        f"Time required to detect and isolate an edit at sequence #500,000 in a {n:,} event ledger",
        tamper_ms,
        "ms",
        f"Linear walk from event 1 to event 500,000 in {n:,} event chain",
        f"Corrupted {n:,} event ledger",
        *arg_src,
        "In-memory linear walk; full rewrite requires anchored verification",
    )

    # 5. Anchored heads, multiple processes, Harpe quality
    gap6_src = ("reports/gap6_argus_anchored_checkpoints.json", "telarch-benchmarks/src/bin/argus_anchored_checkpoints.rs")
    add_claim(
        "argus_anchored_rewrite_caught",
        "Anchored checkpoint verification catches full-chain rewrite attacks",
        gap6["anchored_rewrite_caught"],
        "bool",
        f"{gap6['total_events']:,} events, sequence 5,000 edited and all subsequent hashes recomputed",
        "Adversarially relinked audit chain",
        *gap6_src,
        "Requires the head hash to be kept outside the ledger; the benchmark keeps it in a signed checkpoint",
    )
    add_claim(
        "argus_checkpoint_sign_us",
        "Ed25519 signing latency for a checkpoint note in C2SP signed-note style, signed by the benchmark program (Argus has no checkpoint API)",
        round(gap6["checkpoint_signing_latency_us"], 3),
        "µs",
        "Ed25519 digital signature over domain-tagged checkpoint note",
        "Canonical checkpoint note string",
        *gap6_src,
        "In-memory Ed25519 signature computation; one signature, timed once",
    )
    # Swarm partition: the label follows the measured outcome. One retry per dropped packet does not
    # deliver every message, so under packet loss the scenario shows per-node chain integrity, not
    # convergence, and the claims say so.
    swarm_src = ("reports/swarm_partition.json", "telarch-benchmarks/src/bin/swarm_partition.rs")
    for sc in swarm["scenarios"]:
        loss = sc["packet_loss_pct"]
        total_msgs = sc["total_local_ops"] * (sc["nodes"] - 1)
        # The label says what was shown. With convergence, the statement is the convergence claim;
        # without it, the statement says what still held (each node's own chain) and that the nodes
        # did not converge, so the value "no" reads as a result rather than a failed claim.
        if sc["nodes_converged"]:
            statement = (f"Audit-chain convergence under packet loss: every one of {sc['nodes']} simulated nodes "
                         f"holds every message after reconciliation at {loss}% simulated packet loss")
        else:
            statement = (f"Nodes converged after reconciliation at {loss}% simulated packet loss (no: with one "
                         f"retry per dropped packet the {sc['nodes']} nodes end with different message sets; what "
                         f"holds is per-node chain integrity, every node's own chain verifying)")
        add_claim(
            f"swarm_{loss}pct_converged",
            statement,
            bool(sc["nodes_converged"]),
            "bool",
            f"{sc['nodes']} nodes in one process, {sc['total_local_ops']:,} local mutations, {total_msgs:,} "
            f"deliveries attempted, one retry per dropped packet; {sc['missing_messages']:,} messages missing on "
            f"{sc['nodes_missing_messages']} nodes; every node's own chain verified: {sc['audit_verification_pass']}",
            "Deterministic drop pattern (hash of sequence and target), no real network",
            *swarm_src,
            "Nodes append in arrival order, so their chains differ even when their message sets agree "
            f"({sc['identical_head_hash_pairs']} identical head-hash pairs)",
        )
        add_claim(
            f"swarm_{loss}pct_missing",
            f"Messages that never reached a node after reconciliation at {loss}% simulated packet loss, summed "
            "over nodes",
            sc["missing_messages"],
            "messages",
            f"{sc['nodes']} nodes, {total_msgs:,} deliveries attempted, one retry per dropped packet",
            "Deterministic drop pattern, no real network",
            *swarm_src,
            "A single retry is the program's policy; the figure measures that policy, not a gossip protocol",
        )

    procs = [s["worker_processes"] for s in gap3["multi_process_contention_scenarios"]]
    gap3_src = ("reports/gap3_kibisis_multiproc.json", "scripts/gap3_kibisis_multiproc_crashes.py")
    add_claim(
        "kib_multiproc_split_brain",
        f"Split-brain violations across {min(procs)} to {max(procs)} concurrent OS child processes",
        sum(s["split_brain_violations"] for s in gap3["multi_process_contention_scenarios"]),
        "events",
        f"{min(procs)} to {max(procs)} separate OS child processes competing via SQLiteLeaseCoordinator",
        "SQLite database with WAL mode and BEGIN IMMEDIATE transactions",
        *gap3_src,
        "Coordination uses local SQLite file locking",
    )
    add_claim(
        "kib_stale_writes_rejected",
        "Stale writes accepted by fencing-aware sink after pause past TTL",
        gap3["sigstop_fencing_verification"]["stale_writes_evading_sink"],
        "writes",
        "Holder paused past its TTL (simulated with a sleep; no SIGSTOP signal is sent); higher fencing token issued to successor",
        "Simulated process pause and resume",
        *gap3_src,
        "Requires storage sink to enforce fencing_token > highest_seen_token",
    )
    tasks, seeds, budget = gap1["evaluation_tasks_per_seed"], len(gap1["seeds"]), gap1["budget_ceiling_tokens"]
    add_claim(
        "harpe_quality_recall_pct",
        f"Key fact retention accuracy under {budget}-token budget over {tasks} multi-turn tasks",
        gap1["summary"]["harpe_priority"]["accuracy_pct"]["mean"],
        "%",
        f"{tasks} multi-turn evaluation tasks over {seeds} seeds (N={tasks * seeds} evaluations)",
        "Plant-and-distractor multi-turn context streams",
        "reports/gap1_harpe_quality.json",
        "scripts/gap1_harpe_quality.py",
        "The tasks give the key facts higher priority than the distractors, so priority-ordered selection keeps "
        "them by design; the baselines are simple re-implementations, not LangChain or LlamaIndex code",
    )

    # Facts: the first two are computed from the vendored sources; the others were checked against the code
    deps = direct_dependencies()
    files_scanned, net_hits = network_scan()
    facts = {
        "rust_crates_dependencies": {
            "value": ", ".join(f"{d} {len(v)}" for d, v in deps.items()),
            "status": "verified-by-code",
            "note": "Direct [dependencies] in the vendored Cargo.toml files: "
            + "; ".join(f"{d}: {', '.join(v) or 'none'}" for d, v in deps.items())
            + ".",
        },
        "network_code": {
            "value": len(net_hits),
            "status": "verified-by-code",
            "note": (
                f"No socket, HTTP or process-spawn API found in the vendored source of the three crates "
                f"({files_scanned} .rs files, regex search). Dependencies are not scanned."
                if not net_hits
                else "Socket, HTTP or process-spawn API found in the vendored source: " + ", ".join(net_hits) + "."
            ),
        },
        "kibisis_cipher": {
            "value": "AES-256-GCM entry encryption; SQLCipher whole-file encryption when sqlcipher3 is installed",
            "status": "verified-by-code",
            "note": "Python kibisis at v0.1.0-alpha.4 (src/kibisis/crypto.py and vault.py): entry content is encrypted "
            "with AES-256-GCM under a key derived with PBKDF2-HMAC-SHA256 (600,000 iterations for new envelopes). "
            "With the sqlcipher3 module the package declares, SQLCipher also encrypts the whole database file; "
            "without it the vault opens with plain SQLite pages. The Rust kibisis crate keeps its vault and lease "
            "coordinator in memory and has no encryption implementation.",
        },
        "argus_structure": {
            "value": "Linear SHA-256 hash chain with optional Ed25519 signatures on event hashes",
            "status": "verified-by-code",
            "note": "Argus uses a strictly linear SHA-256 hash chain where each event hash covers sequence, id, "
            "timestamp, type, actor, prev_hash and the SHA-256 digest of the payload.",
        },
        "harpe_algorithm": {
            "value": "Priority-ordered deterministic greedy selection",
            "status": "verified-by-code",
            "note": "Pinned items first, then remaining items in priority order while budget remains. Deterministic; "
            "not an optimal 0/1 knapsack packing.",
        },
    }

    with_deps = {d: len(v) for d, v in deps.items() if v}
    retired = [
        {
            "claim": "Higher token-reduction and lower per-turn time (84.77% / 24.3 µs) plus dollar savings",
            "why": f"The committed report (step1_knapsack_cost_curve.json) shows {claims['harpe_curve_reduction_pct']['value']}% "
            f"and {claims['harpe_curve_avg_us']['value']} µs/turn. Dollar figures depend on assumed provider pricing and "
            "are omitted from headline claims.",
        },
        {
            "claim": "Argus instant tamper detection in tens of microseconds",
            "why": f"The 42 µs figure was an early abort near genesis. Reaching an edit at sequence {tamper_seq:,} in a "
            f"{n:,} event ledger takes {tamper_ms} ms (step3_argus_1m_scale.json).",
        },
        {
            "claim": "Merkle ledger / Merkle chain",
            "why": "Argus is a linear SHA-256 hash chain. Described as that accurately across all surfaces.",
        },
        {
            "claim": "0/1 knapsack optimizer",
            "why": "Harpe uses deterministic priority-ordered greedy selection, not an exact 0/1 integer programming solver.",
        },
        {
            "claim": "Zero-allocation steady state; zero garbage collection",
            "why": "Harpe allocates context items. Absence of garbage collection is an inherent Rust property, not a library feature.",
        },
        {
            "claim": "CMMC Level 2, NIST SP 800-53, or ATO ready",
            "why": "No independent security or compliance assessment has been completed. The libraries are designed for local and disconnected use.",
        },
        {
            "claim": "Unverified PGP fingerprint",
            "why": "PGP key was not published on keys.openpgp.org. Removed until key publication.",
        },
        {
            "claim": "Values, medians and ranges typed into scripts/build_claims.py",
            "why": "Up to v0.1.0-alpha.4 they were typed in from single runs at ca3dff7 and not checked against reports/. "
            "Each value is now read from reports/; a single run has no median or range.",
        },
        {
            "claim": "Rust and Python test counts (36 and 135)",
            "why": "Typed in and not recounted; the per-crate Rust counts given (4, 11 and 23) sum to 38. Each library "
            "repository's CI runs its own tests.",
        },
        {
            "claim": "No third-party crate dependencies (fact value 0)",
            "why": "True for harpe only. Direct dependencies in the vendored Cargo.toml files: "
            + ", ".join(f"{d} {c}" for d, c in with_deps.items())
            + ".",
        },
        {
            "claim": "Lease operations per second under contention (kib_lease16_ops, kib_lease32_ops)",
            "why": "The rate divided every acquire attempt by elapsed time, and about 95% of the attempts were "
            "rejections. It is now published as decisions/s, with successful cycles/s beside it.",
        },
        {
            "claim": "100% audit-chain convergence through 50% simulated packet loss",
            "why": "The program only verified each node's own hash chain. Comparing the per-node message sets shows "
            f"{next(sc['missing_messages'] for sc in swarm['scenarios'] if sc['packet_loss_pct'] == 50):,} messages "
            "missing at 50% loss (swarm_partition.json), so the scenario demonstrates per-node chain integrity "
            "under packet loss, not convergence.",
        },
        {
            "claim": "Reports generated on dedicated pinned performance cores",
            "why": "The benchmark machine is a shared server, not an isolated one: taskset keeps the programs on "
            "their CPUs but does not keep other processes off them.",
        },
        {
            "claim": "Single-bit history corruption trapped at sequence 500,000",
            "why": "The program replaces the payload of event 500,000; it does not flip a single bit.",
        },
        {
            "claim": "SIGSTOP fault injection in the multi-process test",
            "why": "The pause past the TTL is simulated with a sleep; no SIGSTOP signal is sent.",
        },
        {
            "claim": "SQLCipher page-level encryption with PBKDF2-HMAC-SHA256 (600,000 iterations)",
            "why": "The 600,000-iteration PBKDF2 derives the AES-256-GCM key for entry content. SQLCipher encrypts the "
            "whole file only when the sqlcipher3 module is installed.",
        },
    ]

    meta = {
        "generated": bench_env.utc_now(),
        "hardware": hardware,
        "toolchain": toolchain,
        "compiler_flags": FLAGS,
        "benchmark_commit": commit,
        "crate_versions": crates,
        "note": "Each claim value is read from one run of make bench (timestamp_utc in each report). make bench "
        "neither repeats runs nor waits for a quiet machine. The published figures on telarch.ai/evidence are "
        "medians of repeated runs, with every run listed.",
    }

    return {"meta": meta, "claims": claims, "facts": facts, "retired": retired}


REPORT_FILES = (
    "microbenchmarks_phase1.json",
    "step1_knapsack_cost_curve.json",
    "step2_adversarial_contention.json",
    "step3_argus_1m_scale.json",
    "gap1_harpe_quality.json",
    "gap3_kibisis_multiproc.json",
    "gap6_argus_anchored_checkpoints.json",
    "swarm_partition.json",
)


def runs_date(base: str) -> str | None:
    """The date directory under reports/runs/ that claims are built from: TELARCH_RUNS_DATE or the latest."""
    if not os.path.isdir(base):
        return None
    return os.environ.get("TELARCH_RUNS_DATE") or max(
        (d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))), default=None
    )


def one_m_provenance() -> dict:
    """What produced the single 1,000,000-event FileLedger run: reports/runs/<date>/file_ledger_1m/run_env.txt.

    Returns {} when there is no such record. `crate_versions=harpe X, kibisis Y, argus-ledger Z` names the
    vendored versions the run was built from; `commit=` and `pin=` are as run_once.sh writes them.
    """
    base = os.path.join(ROOT, "reports", "runs")
    date = runs_date(base)
    if not date:
        return {}
    path = os.path.join(base, date, "file_ledger_1m", "run_env.txt")
    if not os.path.exists(path):
        return {}
    prov: dict = {"run": os.path.relpath(os.path.dirname(path), ROOT)}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            if key == "crate_versions":
                prov["crate_versions"] = dict(item.strip().rsplit(" ", 1) for item in value.split(",") if item.strip())
            elif key in ("commit", "pin"):
                prov[key] = value.strip()
    return prov


def discover_runs() -> list:
    """Run directories of the latest date under reports/runs/ (or TELARCH_RUNS_DATE) that hold every report."""
    base = os.path.join(ROOT, "reports", "runs")
    date = runs_date(base)
    if not date:
        return []
    runs = []
    for name in sorted(os.listdir(os.path.join(base, date))):
        d = os.path.join(base, date, name)
        if os.path.isdir(d) and all(os.path.exists(os.path.join(d, f)) for f in REPORT_FILES):
            runs.append(d)
    return runs


# Claims read from a one-off report rather than from the repeated runs; aggregate() leaves them at run_count 1.
SINGLE_RUN_CLAIMS = {"argus_file_append_1m_eps"}


def aggregate(output: dict, per_run: list, run_dirs: list) -> None:
    """Replace each numeric claim value with the median across runs; record range and run_count.

    Booleans and strings have no median: they must agree across runs, and a disagreement is recorded in
    the claim's note with the last run's value kept, so it is visible rather than averaged away.
    """
    n = len(per_run)
    for cid, claim in output["claims"].items():
        if cid in SINGLE_RUN_CLAIMS:
            continue
        values = [run[cid]["value"] for run in per_run if cid in run]
        if len(values) != n:
            claim["note"] = (claim["note"] + " " if claim["note"] else "") + f"Present in {len(values)} of {n} runs."
            continue
        claim["run_count"] = n
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            med = statistics.median(values)
            # Keep integers integral: the median of an odd count of integers is one of them, and
            # of an even count may be a .5 that no run produced.
            if all(isinstance(v, int) for v in values) and float(med).is_integer():
                med = int(med)
            claim["value"] = med
            claim["median"] = med
            claim["range"] = [min(values), max(values)]
        elif len(set(map(json.dumps, values))) > 1:
            claim["note"] = (claim["note"] + " " if claim["note"] else "") + (
                "Runs disagree: " + ", ".join(json.dumps(v) for v in values) + "; the last run's value is shown."
            )
    rel = [os.path.relpath(d, ROOT) for d in run_dirs]
    output["meta"]["runs"] = rel
    # The runs recorded how they were pinned; that, not the environment of this script, is what the
    # hardware line must describe.
    pins = set()
    for d in run_dirs:
        try:
            with open(os.path.join(d, "run_env.txt"), encoding="utf-8") as fh:
                pins |= {line[4:].strip() for line in fh if line.startswith("pin=")}
        except OSError:
            pass
    if len(pins) == 1:
        output["meta"]["hardware"] = f"{bench_env.hardware()}; {bench_env.pinning(pins.pop())}"
        for cid, claim in output["claims"].items():
            if cid not in SINGLE_RUN_CLAIMS:  # a one-off report keeps the pin its own run recorded
                claim["hardware"] = output["meta"]["hardware"]
    output["meta"]["note"] = (
        f"Numeric claim values are medians of {n} runs of scripts/run_once.sh ({', '.join(rel)}), range is "
        "[min, max] across them, and reports/ holds the last run. Statement text that quotes counts comes from "
        "the last run. The runs were sequential on one shared machine; run_env.txt in each run directory "
        "records the load average when it started and the commit the programs were built from."
    )


def main():
    output = build(os.path.join(ROOT, "reports"))
    run_dirs = discover_runs()
    if run_dirs:
        aggregate(output, [build(d)["claims"] for d in run_dirs], run_dirs)
    check(output)

    out_path = os.path.join(ROOT, "claims.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    claims, facts, retired = output["claims"], output["facts"], output["retired"]
    print(
        f"Generated {out_path}: {len(claims)} claims, {len(facts)} facts, {len(retired)} retired claims "
        f"(commit {output['meta']['benchmark_commit']}, {len(run_dirs) or 1} run(s))."
    )


if __name__ == "__main__":
    main()
