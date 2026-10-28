#!/usr/bin/env python3
"""Flag figures in claims.json that drifted from a committed baseline.

    scripts/drift_check.py [--baseline baselines/v0.1.0-alpha.9.json] [--current claims.json] [--threshold-pct 10]
    scripts/drift_check.py --make-baseline v0.1.0-alpha.9        # write baselines/<release>.json from claims.json

A figure is flagged when the current value lies outside the baseline's [min, max] range, in the worse
direction, by more than --threshold-pct of the baseline value (bench_figures.py — the same rule
bisect_bench.py applies between two refs). Exit 1 when anything is flagged, so CI stops a PR that
regenerated claims.json with a regressed figure. Both files must come from the same host for the
comparison to mean anything; the baseline records its hardware line and CI compares the committed
claims.json, not figures measured on the runner.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_figures  # noqa: E402

ROOT = HERE.parent


def check(baseline_path, current_path, threshold_pct: float = 10.0) -> dict:
    base = json.loads(Path(baseline_path).read_text())
    cur = json.loads(Path(current_path).read_text())["claims"]
    figures, flagged = {}, []
    for fid, f in base["figures"].items():
        c = cur.get(fid)
        if c is None or not isinstance(c.get("value"), (int, float)) or isinstance(c["value"], bool):
            figures[fid] = {"verdict": "missing-in-current"}
            continue
        ref = {"median": f["value"], "range": list(f["range"]), "n": base["meta"].get("run_count")}
        rng = c.get("range") or [c["value"], c["value"]]
        cand = {"median": c["value"], "range": list(rng), "n": c.get("run_count")}
        v = bench_figures.verdict(ref, cand, f.get("higher_is_better", bench_figures.direction(fid)), threshold_pct)
        figures[fid] = {"baseline": ref, "current": cand, **v}
        if v["verdict"] in bench_figures.FLAGGED:
            flagged.append(fid)
    return {"figures": figures, "flagged": flagged, "threshold_pct": threshold_pct, "baseline": str(baseline_path)}


def make_baseline(release: str, claims_path, out_path) -> dict:
    d = json.loads(Path(claims_path).read_text())
    figures = {}
    for fid, c in d["claims"].items():
        v = c["value"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        figures[fid] = {"value": v, "range": list(c["range"]) if c.get("range") else [v, v], "unit": c["unit"],
                        "run_count": c.get("run_count", 1), "higher_is_better": bench_figures.direction(fid)}
    m = d["meta"]
    runs = m.get("runs") or []
    base = {"meta": {"release": release, "source": "claims.json", "benchmark_commit": m["benchmark_commit"], "generated": m["generated"],
                     "hardware": m["hardware"], "toolchain": m["toolchain"], "crate_versions": m["crate_versions"],
                     "run_count": len(runs) or 1, "runs": runs,
                     "note": "Figures of record for this release: median and [min, max] of the listed runs on the host named in "
                             "'hardware'. drift_check.py compares a later claims.json from the same host against them."},
            "figures": figures}
    Path(out_path).write_text(json.dumps(base, indent=1) + "\n")
    return base


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Flag figures in claims.json that drifted from a committed baseline.")
    ap.add_argument("--baseline", default=str(ROOT / "baselines" / "v0.1.0-alpha.9.json"))
    ap.add_argument("--current", default=str(ROOT / "claims.json"))
    ap.add_argument("--threshold-pct", type=float, default=10.0)
    ap.add_argument("--make-baseline", metavar="RELEASE")
    a = ap.parse_args(argv)
    if a.make_baseline:
        out = ROOT / "baselines" / f"{a.make_baseline}.json"
        b = make_baseline(a.make_baseline, a.current, out)
        print(f"wrote {out}: {len(b['figures'])} figures from {a.current} ({b['meta']['benchmark_commit']}, {b['meta']['run_count']} runs)")
        return 0
    r = check(a.baseline, a.current, a.threshold_pct)
    for fid, f in r["figures"].items():
        if f["verdict"] != "within-noise":
            extra = "" if f["verdict"].startswith("missing") else f" {f['delta_pct']:+.1f}% (noise {f['noise_floor_pct']:.1f}%)"
            print(f"{f['verdict']:18} {fid}{extra}")
    print(f"drift-check vs {Path(a.baseline).name}: {len(r['figures'])} figures, flagged: {r['flagged'] or 'none'}")
    return 1 if r["flagged"] else 0


if __name__ == "__main__":
    sys.exit(main())
