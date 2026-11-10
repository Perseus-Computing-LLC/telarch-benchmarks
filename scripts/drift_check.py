#!/usr/bin/env python3
"""Flag figures in claims.json that drifted from a committed baseline.

    scripts/drift_check.py [--baseline baselines/<release>.json] [--current claims.json] [--threshold-pct 10]
    scripts/drift_check.py --make-baseline v0.1.0-alpha.9        # write baselines/<release>.json from claims.json

A figure is flagged when the current value lies outside the baseline's [min, max] range, in the worse
direction, by more than --threshold-pct of the baseline value (bench_figures.py — the same rule
bisect_bench.py applies between two refs). A figure of record is also flagged when claims.json has no
finite number for it, when its unit is not the baseline's, and when a pass/fail claim is not what it was
at the baseline. A claim whose status is "withdrawn" and whose note says why is the one exception: it is
listed on every run and not flagged. Exit 1 when anything is flagged, so CI stops a PR that regenerated
claims.json with a regressed figure.

Both files must come from the same host for the comparison to mean anything. The baseline records its
hardware line and claims.json records its own; when they are not the same text the comparison is refused
(exit 1). The machine running this script is never looked at: CI compares the committed claims.json, not
figures measured on the runner.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_figures  # noqa: E402

ROOT = HERE.parent


# Pre-release stages in release order; a version without one is the release itself and comes after them.
STAGES = ("alpha", "beta", "rc")


def release_key(stem: str):
    """Sort key of a baseline file stem: v0.1.0-alpha.9 < v0.1.0-alpha.10 < v0.1.0-beta.1 < v0.1.0-rc.1 < v0.1.0."""
    core, _, pre = stem.partition("-")
    stage = re.match(r"[A-Za-z]*", pre).group(0).lower()
    if pre and stage not in STAGES:
        raise SystemExit(f"baselines/{stem}.json: the pre-release stage is not one of {', '.join(STAGES)}")
    return ([int(x) for x in re.findall(r"\d+", core)], STAGES.index(stage) if pre else len(STAGES),
            [int(x) for x in re.findall(r"\d+", pre)])


def latest_baseline() -> Path:
    """The newest baselines/v*.json by version (numeric parts, so alpha.10 sorts after alpha.9; alpha, beta,
    rc, then the release): a re-measure for release N+1 is compared to release N without editing this script."""
    files = sorted((ROOT / "baselines").glob("v*.json"), key=lambda p: release_key(p.stem))
    if not files:
        raise SystemExit("no baselines/v*.json")
    return files[-1]


# Verdicts that fail the gate here, beyond the ones bisect_bench.py also flags.
FLAGGED = bench_figures.FLAGGED + ("missing-in-current", "unit-changed", "pass-fail-changed")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check(baseline_path, current_path, threshold_pct: float = 10.0) -> dict:
    base = json.loads(Path(baseline_path).read_text())
    doc = json.loads(Path(current_path).read_text())
    cur = doc["claims"]
    refused = []
    # What the two files record, not the machine this runs on. A baseline without a hardware line (none of
    # the committed ones) has nothing to compare.
    base_hw, cur_hw = base["meta"].get("hardware"), (doc.get("meta") or {}).get("hardware")
    if base_hw is not None and base_hw != cur_hw:
        refused.append(f"measured under different conditions: baseline {base_hw!r}, current {cur_hw!r}")
    figures, flagged = {}, []
    for fid, f in base["figures"].items():
        c = cur.get(fid)
        if c is not None and c.get("status") == "withdrawn" and isinstance(c.get("note"), str) and c["note"].strip():
            figures[fid] = {"verdict": "withdrawn", "note": c["note"]}
            continue
        if isinstance(f["value"], bool):
            # A pass/fail claim: every run's value has to be the baseline's.
            values = c.get("run_values") or [c.get("value")] if c is not None else []
            same = bool(values) and all(v is f["value"] for v in values)
            figures[fid] = {"verdict": "within-noise" if same else "pass-fail-changed" if values else "missing-in-current"}
        elif c is None or not _finite(c.get("value")) or not all(_finite(x) for x in (c.get("range") or [])):
            figures[fid] = {"verdict": "missing-in-current"}
        elif f.get("unit") is not None and c.get("unit") is not None and f["unit"] != c["unit"]:
            figures[fid] = {"verdict": "unit-changed", "baseline_unit": f["unit"], "current_unit": c["unit"]}
        else:
            ref = {"median": f["value"], "range": list(f["range"]), "n": base["meta"].get("run_count")}
            rng = c.get("range") or [c["value"], c["value"]]
            cand = {"median": c["value"], "range": list(rng), "n": c.get("run_count")}
            v = bench_figures.verdict(ref, cand, f.get("higher_is_better", bench_figures.direction(fid)), threshold_pct)
            figures[fid] = {"baseline": ref, "current": cand, **v}
        if figures[fid]["verdict"] in FLAGGED:
            flagged.append(fid)
    # Claims the baseline has no figure for are compared with nothing; they are named, not passed over.
    unbaselined = sorted(set(cur) - set(base["figures"]))
    return {"figures": figures, "flagged": flagged, "refused": refused, "unbaselined": unbaselined,
            "threshold_pct": threshold_pct, "baseline": str(baseline_path)}


def make_baseline(release: str, claims_path, out_path) -> dict:
    d = json.loads(Path(claims_path).read_text())
    figures, left_out = {}, {}
    for fid, c in d["claims"].items():
        v = c["value"]
        if c.get("status") == "withdrawn":
            left_out[fid] = c.get("note") or "withdrawn"
        elif c.get("status") == "runs-disagree":
            raise SystemExit(f"{fid}: the runs disagree ({c.get('run_values')}); that is not a figure of record")
        elif isinstance(v, bool):
            # Pass/fail: no range and no better direction, the value must stay what it is (None, as for a
            # workload parameter).
            figures[fid] = {"value": v, "range": [v, v], "unit": c["unit"], "run_count": c.get("run_count", 1),
                            "higher_is_better": None}
        elif not isinstance(v, (int, float)):
            left_out[fid] = "text value, not compared"
        else:
            figures[fid] = {"value": v, "range": list(c["range"]) if c.get("range") else [v, v], "unit": c["unit"],
                            "run_count": c.get("run_count", 1), "higher_is_better": bench_figures.direction(fid)}
    m = d["meta"]
    runs = m.get("runs") or []
    base = {"meta": {"release": release, "source": "claims.json", "benchmark_commit": m["benchmark_commit"], "generated": m["generated"],
                     "hardware": m["hardware"], "toolchain": m["toolchain"], "crate_versions": m["crate_versions"],
                     "run_count": len(runs) or 1, "runs": runs, "left_out": left_out,
                     "note": "Figures of record for this release: median and [min, max] of the listed runs on the host named in "
                             "'hardware'. drift_check.py compares a later claims.json from the same host against them."},
            "figures": figures}
    Path(out_path).write_text(json.dumps(base, indent=1) + "\n")
    return base


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Flag figures in claims.json that drifted from a committed baseline.")
    ap.add_argument("--baseline", default=None, help="default: the newest baselines/v*.json by version")
    ap.add_argument("--current", default=str(ROOT / "claims.json"))
    ap.add_argument("--threshold-pct", type=float, default=10.0)
    ap.add_argument("--make-baseline", metavar="RELEASE")
    a = ap.parse_args(argv)
    if a.baseline is None:
        a.baseline = str(latest_baseline())
    if a.make_baseline:
        out = ROOT / "baselines" / f"{a.make_baseline}.json"
        b = make_baseline(a.make_baseline, a.current, out)
        print(f"wrote {out}: {len(b['figures'])} figures from {a.current} ({b['meta']['benchmark_commit']}, {b['meta']['run_count']} runs)")
        return 0
    r = check(a.baseline, a.current, a.threshold_pct)
    for fid, f in r["figures"].items():
        if f["verdict"] != "within-noise":
            if "delta_pct" in f:
                extra = f" {f['delta_pct']:+.1f}% (noise {f['noise_floor_pct']:.1f}%)"
            elif f["verdict"] == "unit-changed":
                extra = f" baseline {f['baseline_unit']}, current {f['current_unit']}"
            else:
                extra = f": {f['note']}" if "note" in f else ""
            print(f"{f['verdict']:18} {fid}{extra}")
    for reason in r["refused"]:
        print(f"refused: {reason}")
    if r["unbaselined"]:
        print(f"not in the baseline, not compared: {', '.join(r['unbaselined'])}")
    print(f"drift-check vs {Path(a.baseline).name}: {len(r['figures'])} figures, flagged: {r['flagged'] or 'none'}")
    return 1 if r["flagged"] or r["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
