"""What the benchmark figures mean when two measurements are compared.

Shared by bisect_bench.py (two refs on one host) and drift_check.py (claims.json against a committed
baseline) so there is one definition of "regressed": the candidate's median lies outside the
reference's [min, max] range, in the worse direction, by more than threshold_pct of the reference
median. "Outside-range" without crossing the threshold is reported but not flagged; the range of
R repeated runs is the noise floor, and a change inside it is not attributable.
"""
import statistics

# True: bigger is better (throughput, recall, reduction). False: smaller is better (latency, errors,
# missing messages). None: a workload parameter — it should not change between refs at all.
HIGHER_IS_BETTER = {
    "harpe_small_p50_us": False, "harpe_large_p50_us": False, "harpe_large_reduction_pct": True,
    "harpe_curve_turns": None, "harpe_curve_reduction_pct": True, "harpe_curve_overruns": False,
    "harpe_curve_avg_us": False, "harpe_curve_harness_us": False,
    "kib_lease16_cycles": True, "kib_lease32_cycles": True, "kib_lease16_decisions": True,
    "kib_lease32_decisions": True, "kib_lease_expiry_recovery_ms": False, "kib_split_brain": False,
    "kib_rollbacks": False, "kib_put_ops": True, "kib_lease8_round_trips": True,
    "argus_events": None, "argus_append_eps": True, "argus_append_us": False,
    "argus_file_append_eps": True, "argus_file_append_1m_eps": True, "argus_verify_eps": True,
    "argus_tamper_seq": None, "argus_tamper_ms": False, "argus_checkpoint_sign_us": False,
    "swarm_0pct_missing": False, "swarm_25pct_missing": False, "swarm_50pct_missing": False,
    "kib_multiproc_split_brain": False, "kib_stale_writes_rejected": False, "harpe_quality_recall_pct": True,
}

# Lease-contention counters: how many lease cycles or round trips N threads or processes complete in a fixed time.
# They follow the host's scheduler and load as much as the code, so a bisect reports them (medians, ranges,
# delta, verdict) and never flags them. kib_lease*_decisions, the split-brain and rollback counts and every other
# figure keep the rule below (telarch phase-P11, item P11.4).
INFORMATIONAL = frozenset({"kib_lease16_cycles", "kib_lease32_cycles", "kib_lease8_round_trips"})


def direction(fid: str, overrides: dict | None = None):
    if overrides and fid in overrides:
        return overrides[fid]
    if fid in HIGHER_IS_BETTER:
        return HIGHER_IS_BETTER[fid]
    for suffix, better in (("_us", False), ("_ms", False), ("_eps", True), ("_ops", True), ("_pct", True),
                           ("_cycles", True), ("_decisions", True), ("_round_trips", True), ("_missing", False)):
        if fid.endswith(suffix):
            return better
    return False


def summarize(values: list) -> dict:
    med = statistics.median(values)
    if all(isinstance(v, int) for v in values) and med == int(med):
        med = int(med)
    return {"median": med, "range": [min(values), max(values)], "n": len(values)}


def verdict(ref: dict, cand: dict, higher_is_better, threshold_pct: float) -> dict:
    """ref/cand: summarize() output. Returns delta_pct, noise_floor_pct, verdict."""
    rm, cm = ref["median"], cand["median"]
    lo, hi = ref["range"]
    scale = abs(rm) if rm else max(abs(cm), abs(hi - lo), 1e-12)   # zero baselines: compare absolutely
    delta_pct = (cm - rm) / scale * 100.0
    noise_floor_pct = (hi - lo) / scale * 100.0
    if higher_is_better is None:
        v = "within-noise" if cm == rm else "parameter-changed"
    elif lo <= cm <= hi:
        v = "within-noise"
    else:
        worse = (cm < lo) if higher_is_better else (cm > hi)
        beyond = (lo - cm) if cm < lo else (cm - hi)
        if not worse:
            v = "improved" if beyond / scale * 100.0 > threshold_pct else "outside-range"
        else:
            v = "regressed" if beyond / scale * 100.0 > threshold_pct else "outside-range"
    return {"delta_pct": delta_pct, "noise_floor_pct": noise_floor_pct, "verdict": v}


FLAGGED = ("regressed", "parameter-changed")
