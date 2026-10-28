#!/usr/bin/env python3
"""Per-commit attribution: measure two refs of this repository on one host, interleaved, and report
per-figure deltas with ranges.

    scripts/bisect_bench.py REF_A REF_B [--rounds 3] [--threshold-pct 10] [--out DIR] [--no-warmup]
    make bisect-bench REF_A=v0.1.0-alpha.9 REF_B=HEAD

Each ref is checked out into its own scratch worktree and built with `cargo build --release`. The
programs then run in A B B A order, `--rounds` times (default 3, so each ref is measured 6 times),
under the same pin as scripts/run_once.sh. ABBA cancels slow drift of the host (thermal, background
load) to first order; the repeated rounds give each figure a [min, max] range, which is the noise
floor on this host — a difference inside it is not attributable to the commit.

A figure is "regressed" when B's median lies outside A's range, in the worse direction, by more than
--threshold-pct of A's median (bench_figures.py). The report is written to --out (default
reports/bisect/<utc>-<A>-<B>/report.json) with every per-run report kept beside it; the summary
table is printed; exit status 1 if any figure regressed or a workload parameter changed.

Figures are read from each run's reports with build_claims.build(), the same code that produces
claims.json, so the figure ids match claims.json and baselines/*.json. A ref whose programs do not
write a report that build_claims expects has that figure recorded as missing, not guessed.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_env  # noqa: E402
import bench_figures  # noqa: E402
import build_claims  # noqa: E402

ROOT = Path(bench_env.ROOT)
PROGRAMS = [
    ["./target/release/telarch-benchmarks", "--json", "reports/microbenchmarks_phase1.json"],
    ["./target/release/knapsack_cost_curve", "--json", "reports/step1_knapsack_cost_curve.json"],
    ["./target/release/adversarial_contention", "--json", "reports/step2_adversarial_contention.json"],
    ["./target/release/argus_1m_scale", "--json", "reports/step3_argus_1m_scale.json", "--file-events",
     os.environ.get("ARGUS_FILE_EVENTS", "100000")],
    ["./target/release/argus_anchored_checkpoints", "--json", "reports/gap6_argus_anchored_checkpoints.json"],
    ["./target/release/restricted_payload_test", "--json", "reports/restricted_payload.json"],
    ["./target/release/swarm_partition", "--json", "reports/swarm_partition.json"],
    ["{python}", "scripts/gap1_harpe_quality.py"],
    ["{python}", "scripts/gap2_harpe_alternatives.py"],
    ["{python}", "scripts/gap3_kibisis_multiproc_crashes.py"],
    ["{python}", "scripts/gap4_kibisis_model_check.py"],
    ["{python}", "scripts/gap5_supply_chain.py"],
    ["{python}", "scripts/gap7_hygiene.py"],
]


def abba_order(rounds: int) -> list:
    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    return ["A", "B", "B", "A"] * rounds


def pin_prefix() -> list:
    pin = ["nice", "-n", os.environ.get("TELARCH_NICE", "15")]
    if shutil.which("ionice"):
        pin += ["ionice", "-c2", "-n7"]
    if shutil.which("taskset"):
        pin += ["taskset", "-c", os.environ.get("TELARCH_CPUSET", "0-9")]
    return pin


def python_for(worktree: Path) -> str:
    venv = Path("/opt/data/work/telarch-suite/.venv/bin/python")
    return str(venv) if venv.exists() else sys.executable


def resolve(ref: str) -> str:
    # ^{commit} peels an annotated tag to the commit it names, so a tag and its commit get one label
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", f"{ref}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()


def checkout(ref: str, sha: str, scratch: Path) -> Path:
    wt = scratch / f"bisect-{sha}"
    if wt.exists():
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt)], capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(wt), sha], check=True, capture_output=True, text=True)
    return wt


def build(wt: Path, log: Path) -> float:
    t = time.time()
    with open(log, "w") as f:
        subprocess.run(["cargo", "build", "--release", "--all-targets"], cwd=wt, check=True, stdout=f, stderr=subprocess.STDOUT,
                       env={**os.environ, "CARGO_BUILD_JOBS": os.environ.get("CARGO_BUILD_JOBS", "4")})
    return time.time() - t


def run_once(wt: Path, dest: Path, pin: list) -> float:
    """Run every program in the worktree and copy its reports to dest. Returns wall seconds."""
    (wt / "reports").mkdir(exist_ok=True)
    (wt / "reports" / "raw_csvs").mkdir(exist_ok=True)
    env = {**os.environ, "TELARCH_PIN": " ".join(pin), "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    t = time.time()
    for prog in PROGRAMS:
        cmd = [p.replace("{python}", python_for(wt)) for p in prog]
        subprocess.run(pin + cmd, cwd=wt, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dest.mkdir(parents=True, exist_ok=True)
    for name in build_claims.REPORT_FILES:
        src = wt / "reports" / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return time.time() - t


def figures_of(report_dir: Path) -> dict:
    """{figure_id: numeric value} from one run's reports via build_claims; non-numeric claims skipped."""
    try:
        claims = build_claims.build(str(report_dir))["claims"]
    except (FileNotFoundError, KeyError, TypeError) as e:
        print(f"  warning: {report_dir.name}: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    return {k: c["value"] for k, c in claims.items() if isinstance(c["value"], (int, float)) and not isinstance(c["value"], bool)}


def compare(a_runs: list, b_runs: list, threshold_pct: float, higher_is_better: dict | None = None) -> dict:
    """a_runs/b_runs: [{figure_id: value}, ...]. Per-figure summary, verdicts and the flagged list."""
    ids = sorted(set().union(*a_runs, *b_runs))
    figures, flagged = {}, []
    for fid in ids:
        av = [r[fid] for r in a_runs if fid in r]
        bv = [r[fid] for r in b_runs if fid in r]
        if not av or not bv:
            figures[fid] = {"verdict": "missing-in-a" if not av else "missing-in-b",
                            "a": bench_figures.summarize(av) if av else None, "b": bench_figures.summarize(bv) if bv else None}
            continue
        a, b = bench_figures.summarize(av), bench_figures.summarize(bv)
        v = bench_figures.verdict(a, b, bench_figures.direction(fid, higher_is_better), threshold_pct)
        figures[fid] = {"a": a, "b": b, "higher_is_better": bench_figures.direction(fid, higher_is_better), **v}
        if v["verdict"] in bench_figures.FLAGGED:
            flagged.append(fid)
    return {"figures": figures, "flagged": flagged, "threshold_pct": threshold_pct}


def table(result: dict, a: str, b: str) -> str:
    lines = [f"{'figure':32} {'A='+a:>14} {'B='+b:>14} {'delta%':>8} {'noise%':>7}  verdict",
             "-" * 92]
    for fid, f in result["figures"].items():
        if f["verdict"].startswith("missing"):
            lines.append(f"{fid:32} {'-':>14} {'-':>14} {'':>8} {'':>7}  {f['verdict']}")
            continue
        lines.append(f"{fid:32} {f['a']['median']:>14,} {f['b']['median']:>14,} {f['delta_pct']:>+8.1f} {f['noise_floor_pct']:>7.1f}  {f['verdict']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure two refs on one host, ABBA-interleaved, and report per-figure deltas.")
    ap.add_argument("ref_a")
    ap.add_argument("ref_b")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--threshold-pct", type=float, default=10.0)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-warmup", action="store_true", help="skip the unscored first run of each ref")
    ap.add_argument("--keep-worktrees", action="store_true")
    a = ap.parse_args(argv)

    sha = {"A": resolve(a.ref_a), "B": resolve(a.ref_b)}
    started = bench_env.utc_now()
    out = a.out or ROOT / "reports" / "bisect" / f"{started.replace(':', '').replace('-', '')}-{sha['A']}-{sha['B']}"
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "telarch-bisect"
    scratch.mkdir(parents=True, exist_ok=True)
    pin = pin_prefix()
    print(f"bisect-bench A={a.ref_a} ({sha['A']})  B={a.ref_b} ({sha['B']})  rounds={a.rounds}  pin={' '.join(pin)}")

    wt, build_s = {}, {}
    for side in ("A", "B"):
        wt[side] = checkout(a.__dict__[f"ref_{side.lower()}"], sha[side], scratch)
        build_s[side] = build(wt[side], out / f"build-{side}.log")
        print(f"  built {side} {sha[side]} in {build_s[side]:.0f}s")

    runs = {"A": [], "B": []}
    durations = []
    try:
        if not a.no_warmup:
            for side in ("A", "B"):
                d = run_once(wt[side], out / f"warmup-{side}", pin)
                print(f"  warm-up {side}: {d:.0f}s (not scored)")
        for i, side in enumerate(abba_order(a.rounds), 1):
            dest = out / f"run{i:02d}-{side}"
            d = run_once(wt[side], dest, pin)
            durations.append(d)
            figs = figures_of(dest)
            runs[side].append(figs)
            print(f"  run {i:02d} {side}: {d:.0f}s, {len(figs)} figures, loadavg {open('/proc/loadavg').read().split()[0]}")
    finally:
        if not a.keep_worktrees:
            for side in ("A", "B"):
                subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt[side])], capture_output=True)

    result = compare(runs["A"], runs["B"], a.threshold_pct)
    result["meta"] = {
        "started_utc": started, "finished_utc": bench_env.utc_now(), "ref_a": a.ref_a, "ref_b": a.ref_b, "sha": sha,
        "rounds": a.rounds, "order": abba_order(a.rounds), "warmup": not a.no_warmup, "hardware": bench_env.hardware(),
        "pin": " ".join(pin), "toolchain": bench_env.toolchain(), "build_seconds": {k: round(v) for k, v in build_s.items()},
        "run_seconds_median": round(statistics.median(durations)) if durations else None,
        "argus_file_events": int(os.environ.get("ARGUS_FILE_EVENTS", "100000")),
    }
    (out / "report.json").write_text(json.dumps(result, indent=1) + "\n")
    print(table(result, sha["A"], sha["B"]))
    print(f"\nflagged: {result['flagged'] or 'none'}   report: {out / 'report.json'}")
    return 1 if result["flagged"] else 0


if __name__ == "__main__":
    sys.exit(main())
