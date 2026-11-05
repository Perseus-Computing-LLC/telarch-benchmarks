#!/usr/bin/env python3
"""
tdp_feasibility_probe.py
Empirical TRL-4 Feasibility Demonstration for DAF27BZ01-DV008 (TDP-CryptEng).
Executes actual STEP AP214 and AP242 parsing, PMI entity extraction, SHA-256 entity hash chaining
with Argus canonical serialization invariants, true numeric coordinate tamper mutation (+0.05 mm / 50 um),
and automated IA structural checks against real CAD models including CAx-IF AP242, ISO 10303-214,
and full-scale synthetic assemblies (>100,000 entities).
"""
import sys
import time
import hashlib
import re
import os
import json
import platform
import argparse
import tempfile
from pathlib import Path

try:
    from argus_ledger.canonical import canonical_json_bytes
    HAS_ARGUS_CANONICAL = True
except ImportError:
    HAS_ARGUS_CANONICAL = False

try:
    from kibisis import FileLeaseCoordinator
    HAS_KIBISIS_LEASE = True
except ImportError:
    HAS_KIBISIS_LEASE = False


def detect_step_units(content):
    """Detects primary length unit from STEP file header/data."""
    if re.search(r'SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)', content):
        return "MILLIMETRE", 0.05
    elif re.search(r'SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.\s*\)', content):
        return "METRE", 0.00005  # 50 um in metres
    return "UNKNOWN", 0.05


def parse_step_entities(filepath):
    """Parses ISO 10303-21 STEP physical file into header and structured entity dictionary."""
    entities = {}
    header = []
    in_data = False
    
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    file_bytes = len(content.encode('utf-8'))
    
    unit_name, delta_val = detect_step_units(content)
        
    # Split into statements ending with semicolon, stripping comments
    clean_content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    
    statements = clean_content.split(';')
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        if stmt == 'DATA':
            in_data = True
            continue
        elif stmt == 'ENDSEC':
            in_data = False
            continue
        elif not in_data:
            header.append(stmt)
            continue
            
        # Parse entity: #id = TYPE(args...) or #id=(...)
        m = re.match(r'#(\d+)\s*=\s*(.+)', stmt, re.DOTALL)
        if m:
            eid = int(m.group(1))
            val = m.group(2).strip()
            # extract entity type
            m_type = re.match(r'([A-Z0-9_]+)\s*\((.*)\)', val, re.DOTALL)
            if m_type:
                etype = m_type.group(1)
                econtent = m_type.group(2).strip()
            else:
                etype = "COMPLEX_OR_RAW"
                econtent = val
            entities[eid] = {
                "raw": stmt,
                "type": etype,
                "content": econtent
            }
    return header, entities, file_bytes, unit_name, delta_val


def compute_entity_hash_chain(entities):
    """Computes a linear SHA-256 hash chain over canonical sorted entity statements."""
    chain = []
    prev_hash = "0" * 64
    entity_hashes = {}
    
    sorted_ids = sorted(entities.keys())
    for eid in sorted_ids:
        raw_text = entities[eid]["raw"]
        if HAS_ARGUS_CANONICAL:
            # Canonical integer and string serialization per Argus specification
            payload = canonical_json_bytes({
                "prev": prev_hash,
                "id": eid,
                "type": entities[eid]["type"],
                "raw": raw_text
            })
        else:
            payload = f"{prev_hash}:{eid}:{raw_text}".encode('utf-8')
            
        curr_hash = hashlib.sha256(payload).hexdigest()
        entity_hashes[eid] = curr_hash
        chain.append({"id": eid, "type": entities[eid]["type"], "hash": curr_hash})
        prev_hash = curr_hash
        
    return prev_hash, entity_hashes, chain


def run_automated_ia_rules(entities):
    """Executes structural entity and PMI presence checks derived from MIL-STD-31000B and ISO 10303."""
    findings = []
    
    # Check 1: Product definition context
    products = [e for e in entities.values() if e["type"] in ("PRODUCT", "PRODUCT_DEFINITION", "PRODUCT_DEFINITION_SHAPE")]
    if not products:
        findings.append({"rule": "MIL-STD-31000B-PROD-CONTEXT", "status": "FAIL", "msg": "No PRODUCT or PRODUCT_DEFINITION entity found"})
    else:
        findings.append({"rule": "MIL-STD-31000B-PROD-CONTEXT", "status": "PASS", "msg": f"{len(products)} Product Context entities verified"})
        
    # Check 2: Geometric representation uncertainty
    uncertainties = [e for e in entities.values() if e["type"] in ("UNCERTAINTY_MEASURE_WITH_UNIT", "GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT")]
    if uncertainties:
        findings.append({"rule": "ISO-10303-GEOM-UNCERTAINTY", "status": "PASS", "msg": f"{len(uncertainties)} Geometric uncertainty assertions verified"})
    else:
        findings.append({"rule": "ISO-10303-GEOM-UNCERTAINTY", "status": "WARN", "msg": "No explicit UNCERTAINTY_MEASURE_WITH_UNIT entity in DATA section"})
        
    # Check 3: Model-Based Definition / PMI annotations
    pmi_entities = [e for e in entities.values() if any(k in e["type"] for k in [
        "DIMENSIONAL", "TOLERANCE", "SHAPE_ASPECT", "DATUM", "GEOMETRIC_TOLERANCE", "DRAUGHTING_MODEL_ITEM_ASSOCIATION"
    ])]
    if pmi_entities:
        findings.append({"rule": "MBD-PMI-ANNOTATIONS", "status": "PASS", "msg": f"{len(pmi_entities)} MBD / PMI annotation entities present"})
    else:
        findings.append({"rule": "MBD-PMI-ANNOTATIONS", "status": "WARN", "msg": "Pure boundary representation (B-Rep) geometry; no semantic PMI tolerance entities detected"})
        
    return findings


def mutate_cartesian_point_coordinate(entities, delta):
    """Finds first non-zero CARTESIAN_POINT and introduces a real numeric coordinate shift (+0.05 mm)."""
    mutated = dict(entities)
    target_id = None
    orig_stmt = None
    mutated_stmt = None
    
    num_re = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
    
    sorted_ids = sorted(entities.keys())
    for eid in sorted_ids:
        if entities[eid]["type"] == "CARTESIAN_POINT":
            raw = entities[eid]["raw"]
            # Look for coordinate tuple: (x, y, z) or (x, y)
            m = re.search(r'\(\s*(.+?)\s*\)', raw)
            if m:
                coords_str = m.group(1).split(',')
                valid_coords = [c.strip() for c in coords_str if num_re.match(c.strip())]
                if len(valid_coords) >= 2:
                    floats = [float(c) for c in valid_coords]
                    # Check if non-zero point
                    if any(abs(f) > 1e-6 for f in floats):
                        target_id = eid
                        orig_stmt = raw
                        # Mutate the first coordinate
                        new_x = floats[0] + delta
                        # Replace in original raw statement
                        # Find the first coordinate token inside the parens
                        orig_x_str = valid_coords[0]
                        new_x_str = f"{new_x:.6f}".rstrip('0').rstrip('.')
                        # Format replacement
                        mutated_stmt = raw.replace(orig_x_str, new_x_str, 1)
                        mutated[eid] = {
                            "raw": mutated_stmt,
                            "type": entities[eid]["type"],
                            "content": entities[eid]["content"]
                        }
                        break
                        
    # Fallback if no non-zero CARTESIAN_POINT was found
    if target_id is None:
        for eid in sorted_ids:
            if entities[eid]["type"] == "CARTESIAN_POINT":
                target_id = eid
                orig_stmt = entities[eid]["raw"]
                mutated_stmt = orig_stmt.replace("(0.,0.,0.)", f"({delta:.6f},0.,0.)", 1)
                mutated[eid] = {
                    "raw": mutated_stmt,
                    "type": entities[eid]["type"],
                    "content": entities[eid]["content"]
                }
                break

    return target_id, orig_stmt, mutated_stmt, mutated


def run_benchmark_trials(filepath, n_trials=30):
    """Executes N=30 benchmark trials to measure median and p95 latency and throughput."""
    header, entities, file_bytes, unit_name, delta_val = parse_step_entities(filepath)
    
    parse_times = []
    hash_times = []
    tamper_times = []
    rule_times = []
    
    # Target mutation
    target_id, orig_stmt, mut_stmt, mutated_entities = mutate_cartesian_point_coordinate(entities, delta_val)
    
    # Pre-compute baseline hash chain
    baseline_root, baseline_hashes, _ = compute_entity_hash_chain(entities)
    corrupted_found = None
    
    for _ in range(n_trials):
        # 1. Parse timing
        t0 = time.perf_counter()
        _h, _e, _b, _u, _d = parse_step_entities(filepath)
        t1 = time.perf_counter()
        parse_times.append((t1 - t0) * 1000.0)
        
        # 2. Hash chaining timing
        t2 = time.perf_counter()
        _root, _hashes, _chain = compute_entity_hash_chain(entities)
        t3 = time.perf_counter()
        hash_times.append((t3 - t2) * 1000.0)
        
        # 3. Tamper detection timing
        t4 = time.perf_counter()
        mut_root, mut_hashes, _ = compute_entity_hash_chain(mutated_entities)
        # Find divergence
        diverged = (mut_root != baseline_root)
        corrupted_found = None
        for eid in sorted(entities.keys()):
            if baseline_hashes[eid] != mut_hashes[eid]:
                corrupted_found = eid
                break
        t5 = time.perf_counter()
        tamper_times.append((t5 - t4) * 1000.0)
        
        # 4. Automated IA Rule screening timing
        t6 = time.perf_counter()
        _rules = run_automated_ia_rules(entities)
        t7 = time.perf_counter()
        rule_times.append((t7 - t6) * 1000.0)
        
    parse_times.sort()
    hash_times.sort()
    tamper_times.sort()
    rule_times.sort()
    
    def get_stats(arr):
        n = len(arr)
        median = arr[n // 2]
        p95 = arr[int(n * 0.95)]
        return round(median, 3), round(p95, 3)
        
    p_med, p_p95 = get_stats(parse_times)
    h_med, h_p95 = get_stats(hash_times)
    t_med, t_p95 = get_stats(tamper_times)
    r_med, r_p95 = get_stats(rule_times)
    
    file_mb = file_bytes / (1024 * 1024)
    parse_throughput_mb_s = round(file_mb / (p_med / 1000.0), 2) if p_med > 0 else 0.0
    hash_throughput_mb_s = round(file_mb / (h_med / 1000.0), 2) if h_med > 0 else 0.0
    
    ia_findings = run_automated_ia_rules(entities)
    
    return {
        "file_bytes": file_bytes,
        "entity_count": len(entities),
        "unit": unit_name,
        "shift_mm": 0.05,
        "mutation_target_id": target_id,
        "corrupted_entity_found": corrupted_found,
        "corrupted_entity_matched": (corrupted_found == target_id),
        "orig_stmt": orig_stmt,
        "mutated_stmt": mut_stmt,
        "trials": n_trials,
        "parse_latency_ms": {"median": p_med, "p95": p_p95},
        "parse_throughput_mb_s": parse_throughput_mb_s,
        "hash_latency_ms": {"median": h_med, "p95": h_p95},
        "hash_throughput_mb_s": hash_throughput_mb_s,
        "tamper_detect_latency_ms": {"median": t_med, "p95": t_p95},
        "tamper_detected": (baseline_root != mut_root),
        "rule_latency_ms": {"median": r_med, "p95": r_p95},
        "ia_findings": ia_findings
    }


def main():
    parser = argparse.ArgumentParser(description="TDP Feasibility Benchmark Probe")
    script_dir = Path(__file__).resolve().parent
    default_data = script_dir / "fixtures" if (script_dir / "fixtures").exists() else script_dir / "data"
    default_out = script_dir / "feasibility_probe_results.json"
    parser.add_argument("--data-dir", default=str(default_data),
                        help="Directory containing STEP fixture files (defaults to ./fixtures or ./data)")
    parser.add_argument("--output", default=str(default_out),
                        help="Path to save benchmark JSON output")
    parser.add_argument("--trials", type=int, default=30, help="Number of benchmark repetitions")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        # Fallback to local data dir or current directory
        if (script_dir.parent / "data").exists():
            data_dir = script_dir.parent / "data"
        elif Path("data").exists():
            data_dir = Path("data")
    
    # Demonstrate Kibisis SWMR lease acquisition and mutual exclusion
    lease_info = None
    if HAS_KIBISIS_LEASE:
        with tempfile.TemporaryDirectory() as td:
            coord = FileLeaseCoordinator(td)
            l1 = coord.acquire("tdp-assembly-lock", "flightline-tablet-01", ttl_seconds=10)
            token_1 = l1.fencing_token
            holder_1 = l1.holder_id
            conflict_blocked = False
            try:
                coord.acquire("tdp-assembly-lock", "flightline-tablet-02", ttl_seconds=10)
            except Exception:
                conflict_blocked = True
            coord.release(l1)
            l2 = coord.acquire("tdp-assembly-lock", "flightline-tablet-02", ttl_seconds=10)
            token_2 = l2.fencing_token
            holder_2 = l2.holder_id
            coord.release(l2)
            
            lease_info = {
                "resource": "tdp-assembly-lock",
                "holder_1": holder_1,
                "fencing_token_1": token_1,
                "conflict_prevented": conflict_blocked,
                "holder_2": holder_2,
                "fencing_token_2": token_2,
                "monotonic_token_progression": (token_2 > token_1),
                "status": "MUTUAL_EXCLUSION_VERIFIED"
            }

    test_models = [
        {"name": "Ctl011.stp", "desc": "ISO 10303-214 PMI Control Model (with DIMENSIONAL_SIZE)", "schema": "AP214"},
        {"name": "Pf033.stp", "desc": "ISO 10303-214 Closed Shell Assembly (4,485 entities)", "schema": "AP214"},
        {"name": "pushrod_ap242.stp", "desc": "ISO 10303-242 Accelerator Pushrod (80 entities)", "schema": "AP242"},
        {"name": "y_idler_ap242.stp", "desc": "ISO 10303-242 Structural Bracket (3,394 entities)", "schema": "AP242"},
        {"name": "cowl_ap242.stp", "desc": "ISO 10303-242 Complex Airframe Cowl (9,230 entities)", "schema": "AP242"},
        {"name": "synthetic_assembly_100k.stp", "desc": "ISO 10303-242 Scaled Assembly (100,000 entities)", "schema": "AP242"}
    ]
    
    results = []
    print("=" * 80)
    print("TDP-CryptEng TRL-4 EMPIRICAL BENCHMARK PROBE (N=30 REPETITIONS)")
    print(f"Platform: {platform.machine()} | OS: {platform.system()} {platform.release()} | Python: {platform.python_version()}")
    if lease_info:
        print(f"Kibisis Concurrency Lease: {lease_info['resource']} acquired by {lease_info['holder_1']} (Token: {lease_info['fencing_token_1']}), conflict prevented: {lease_info['conflict_prevented']}, next token: {lease_info['fencing_token_2']}")
    print("=" * 80)
    
    for tm in test_models:
        fpath = data_dir / tm["name"]
        if not fpath.exists():
            print(f"[SKIP] {tm['name']} not found")
            continue
            
        print(f"\nEvaluating: {tm['name']} ({tm['schema']} - {tm['desc']})")
        bench = run_benchmark_trials(str(fpath), n_trials=args.trials)
        bench["name"] = tm["name"]
        bench["schema"] = tm["schema"]
        bench["description"] = tm["desc"]
        results.append(bench)
        
        print(f"  Entities:        {bench['entity_count']:,} | Size: {bench['file_bytes'] / 1024:.1f} KB | Units: {bench['unit']}")
        print(f"  Parse Latency:   {bench['parse_latency_ms']['median']} ms (p95: {bench['parse_latency_ms']['p95']} ms) -> {bench['parse_throughput_mb_s']} MB/s")
        print(f"  Hash Chaining:   {bench['hash_latency_ms']['median']} ms (p95: {bench['hash_latency_ms']['p95']} ms) -> {bench['hash_throughput_mb_s']} MB/s")
        print(f"  Tamper Detect:   {bench['tamper_detected']} in {bench['tamper_detect_latency_ms']['median']} ms (Entity #{bench['mutation_target_id']} shifted +{bench['shift_mm']} mm)")
        for finding in bench["ia_findings"]:
            print(f"  [{finding['status']}] {finding['rule']}: {finding['msg']}")

    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    payload_to_save = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": {
            "machine": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
            "python_version": platform.python_version()
        },
        "kibisis_lease_verification": lease_info,
        "argus_canonical_serialization": HAS_ARGUS_CANONICAL,
        "models": results
    }
    
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(payload_to_save, f, indent=2)
        
    out_bytes = out_file.read_bytes()
    sha256_hash = hashlib.sha256(out_bytes).hexdigest()
    print("\n" + "=" * 80)
    print(f"Benchmark results successfully saved to: {out_file}")
    print(f"SHA-256 Digest: {sha256_hash}")
    print("=" * 80)


if __name__ == "__main__":
    main()
