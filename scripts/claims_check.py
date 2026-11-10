#!/usr/bin/env python3
"""Keep the benchmark numbers in README.md equal to claims.json.

README.md carries one block between `<!-- claims:start -->` and `<!-- claims:end -->` that is rendered
from claims.json by this script, so every number in it is a claim value, a claim's range, or the run
count. `make claims-check` re-renders the block and fails when the README differs from it; `--write`
replaces the block. The check also fails when claims.json is not what build_claims.py makes of the
committed reports (`generated` aside) or is not valid against claims_schema.json, so a value typed into
claims.json and re-rendered does not pass. It fails when the README names a vendored crate at a version other
than the one claims.json records, when any line outside the block quotes a retired claim's value
(the numbers a reader is most likely to copy from an older README), and when README.md or
evals/README.md uses a retired product name.

Everything else in README.md (evaluation scores from evals/README.md, prose) is outside the block and
is not derived from claims.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
START, END = "<!-- claims:start -->", "<!-- claims:end -->"

# Product names no longer used in public writing. "Perseus Computing" is the company and stays, as does the
# Codex marketplace name `perseus-telarch-plugins`. The same rule guards telarch.ai and perseuscomputing.com.
RETIRED_NAMES = re.compile(r"(?i)\bperseus\b(?![\s_-]*computing)(?!-telarch-plugins\b)|\bmimir\b|\bplutus\b|\bmneme\b")
# The published prose: the figures in these two files are the ones readers quote.
PROSE = [README, os.path.join(ROOT, "evals", "README.md")]

# (crate shown, claim id, program or script that produced it). The order is the order of the table.
ROWS = [
    ("Harpe", "harpe_curve_reduction_pct", "knapsack_cost_curve"),
    ("Harpe", "harpe_curve_overruns", "knapsack_cost_curve"),
    ("Harpe", "harpe_curve_avg_us", "knapsack_cost_curve"),
    ("Harpe", "harpe_curve_harness_us", "knapsack_cost_curve"),
    ("Harpe", "harpe_small_p50_us", "telarch-benchmarks"),
    ("Harpe", "harpe_large_p50_us", "telarch-benchmarks"),
    ("Harpe", "harpe_quality_recall_pct", "scripts/gap1_harpe_quality.py"),
    ("Kibisis", "kib_split_brain", "adversarial_contention"),
    ("Kibisis", "kib_rollbacks", "adversarial_contention"),
    ("Kibisis", "kib_lease16_cycles", "adversarial_contention"),
    ("Kibisis", "kib_lease16_decisions", "adversarial_contention"),
    ("Kibisis", "kib_lease32_cycles", "adversarial_contention"),
    ("Kibisis", "kib_lease32_decisions", "adversarial_contention"),
    ("Kibisis", "kib_lease_expiry_recovery_ms", "adversarial_contention"),
    ("Kibisis", "kib_lease8_round_trips", "telarch-benchmarks"),
    ("Kibisis", "kib_put_ops", "telarch-benchmarks"),
    ("Kibisis", "kib_multiproc_split_brain", "scripts/gap3_kibisis_multiproc_crashes.py"),
    ("Kibisis", "kib_stale_writes_rejected", "scripts/gap3_kibisis_multiproc_crashes.py"),
    ("Argus", "argus_append_eps", "argus_1m_scale"),
    ("Argus", "argus_file_append_eps", "argus_1m_scale"),
    ("Argus", "argus_file_append_1m_eps", "argus_1m_scale"),
    ("Argus", "argus_verify_eps", "argus_1m_scale"),
    ("Argus", "argus_tamper_ms", "argus_1m_scale"),
    ("Argus", "argus_anchored_rewrite_caught", "argus_anchored_checkpoints"),
    ("Argus", "swarm_0pct_converged", "swarm_partition"),
    ("Argus", "swarm_25pct_converged", "swarm_partition"),
    ("Argus", "swarm_25pct_missing", "swarm_partition"),
    ("Argus", "swarm_50pct_converged", "swarm_partition"),
    ("Argus", "swarm_50pct_missing", "swarm_partition"),
]


def fmt(value, unit: str) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if unit in ("µs", "ms") else f"{value:,.1f}"
    return str(value)


# Claim statuses that must not render like a plain measured value; each also gets a line under the table.
REMARKED = {"withdrawn": "withdrawn", "runs-disagree": "runs disagree"}


def value_cell(c: dict) -> str:
    if c.get("status") == "withdrawn":
        return "withdrawn"
    if c.get("status") == "runs-disagree":
        return "runs disagree: " + ", ".join(fmt(v, c["unit"]) for v in c.get("run_values", [c["value"]]))
    unit = "" if c["unit"] == "bool" else f" {c['unit']}"
    return f"{fmt(c['value'], c['unit'])}{unit}"


def report_differences(claims: dict) -> list[str]:
    """Where `claims` differs from what build_claims.py makes of the reports on disk.

    meta.generated is the time of a build and is not compared. With run directories everything else is a
    function of the committed reports, run_env.txt files and vendored Cargo.toml files, so it is the same on
    every machine. Without any, reports/ carries no record of the commit, toolchain or pinning, build_claims
    takes those from its own invocation, and they are not compared either.
    """
    scripts = os.path.dirname(os.path.abspath(__file__))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import build_claims

    out = []
    try:
        build_claims.validate(claims)
    except Exception as e:  # noqa: BLE001 - jsonschema's error type when it is installed, AssertionError otherwise
        out.append(f"claims.json is not valid against claims_schema.json: {str(e).splitlines()[0]}")
    try:
        rebuilt, run_dirs = build_claims.rebuild()
        rebuilt = json.loads(json.dumps(rebuilt))
    except (OSError, KeyError, ValueError) as e:
        return out + [f"claims.json cannot be rebuilt from the reports: {type(e).__name__}: {e}"]
    skipped = {"generated"} if run_dirs else {"generated", "benchmark_commit", "commit", "method_link", "toolchain", "hardware"}

    def walk(have, want, path: str) -> None:
        if isinstance(have, dict) and isinstance(want, dict):
            for k in sorted(set(have) | set(want)):
                if k in skipped and (path == "meta" or path.startswith("claims.")):
                    continue
                if k not in have or k not in want:
                    out.append(f"{path}.{k} is {'missing from' if k not in have else 'only in'} claims.json")
                else:
                    walk(have[k], want[k], f"{path}.{k}" if path else k)
        elif have != want or type(have) is not type(want):
            out.append(f"{path} is {json.dumps(have)[:80]} in claims.json; the reports give {json.dumps(want)[:80]}")

    walk(claims, rebuilt, "")
    return out


def render(claims: dict) -> str:
    meta, table = claims["meta"], claims["claims"]
    runs = meta.get("runs", [])
    versions = ", ".join(f"{c} {v}" for c, v in meta["crate_versions"].items())
    lines = [START]
    if runs:
        # More than one commit: the runs were not all built from the same tree, and the header says so.
        commits = meta["benchmark_commit"].split(", ")
        at = "at commit" if len(commits) == 1 else "built from the commits each run recorded,"
        lines.append(
            f"Medians of {len(runs)} sequential runs of `scripts/run_once.sh` on {meta['hardware']}, "
            f"{meta['toolchain']}, `{meta['compiler_flags']}`, with synthetic data, {at} "
            f"{', '.join(f'`{c}`' for c in commits)} ({versions}). The range column is [min, max] across the runs; the raw "
            f"reports of every run are under `{os.path.dirname(runs[0])}/`. The machine was shared and not idle "
            f"(`run_env.txt` in each run directory records the load average when it started), so these are "
            f"reproducible-on-this-host numbers, not a quiet-machine ceiling."
        )
    else:
        lines.append(
            f"One run of `make bench` on {meta['hardware']}, {meta['toolchain']}, `{meta['compiler_flags']}`, "
            f"with synthetic data, at commit `{meta['benchmark_commit']}` ({versions})."
        )
    lines += ["", "| Crate | Claim | Value | Runs | Range | Program |", "| :--- | :--- | ---: | :---: | :--- | :--- |"]
    for crate, cid, program in ROWS:
        c = table[cid]
        rng = "" if c["range"] is None else f"{fmt(c['range'][0], c['unit'])} to {fmt(c['range'][1], c['unit'])}"
        lines.append(
            f"| {crate} | {c['statement']} | {value_cell(c)} | {c['run_count']} | {rng} | `{program}` |"
        )
    for _, cid, _ in ROWS:
        c = table[cid]
        if c.get("status") in REMARKED:
            lines += ["", f"`{cid}` ({REMARKED[c['status']]}): {c.get('note', '')}"]
    lines.append("")
    lines.append(
        "Each claim's scope, dataset and limits are in [`claims.json`](claims.json); `make claims-check` fails when "
        "this block differs from it."
    )
    lines.append(END)
    return "\n".join(lines)


def split(text: str) -> tuple[str, str, str]:
    a, b = text.find(START), text.find(END)
    if a < 0 or b < 0 or b < a:
        raise SystemExit(f"README.md has no {START} ... {END} block")
    return text[:a], text[a : b + len(END)], text[b + len(END) :]


def number_tokens(text: str) -> set[str]:
    """Numbers as a reader would copy them: thousands separators kept, trailing zeros as written."""
    return set(re.findall(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d+\.\d+|(?<![\w.,])\d{4,}", text))


def retired_values(claims: dict) -> set[str]:
    out = set()
    for entry in claims.get("retired", []):
        out |= number_tokens(entry.get("claim", ""))
    return out


def retired_names(text: str) -> list[tuple[int, str]]:
    """(line number, name) for each retired product name in `text`."""
    return [(text.count("\n", 0, m.start()) + 1, m.group(0)) for m in RETIRED_NAMES.finditer(text)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="replace the README block instead of checking it")
    args = ap.parse_args()
    with open(os.path.join(ROOT, "claims.json"), encoding="utf-8") as fh:
        claims = json.load(fh)
    with open(README, encoding="utf-8") as fh:
        text = fh.read()
    before, block, after = split(text)
    wanted = render(claims)
    if args.write:
        with open(README, "w", encoding="utf-8") as fh:
            fh.write(before + wanted + after)
        print("claims-check: README block rewritten")
        return 0

    failures = []
    if block != wanted:
        have, want = block.splitlines(), wanted.splitlines()
        for i, (h, w) in enumerate(zip(have, want)):
            if h != w:
                failures.append(f"block line {i + 1} differs:\n    README: {h}\n    claims: {w}")
                break
        if len(have) != len(want):
            failures.append(f"block has {len(have)} lines, claims.json renders {len(want)}")
    differences = report_differences(claims)
    failures += differences[:10]
    if len(differences) > 10:
        failures.append(f"... and {len(differences) - 10} more differences between claims.json and the reports")
    outside = before + after
    for crate, version in claims["meta"]["crate_versions"].items():
        # Cargo versions only: a PyPI label such as `harpe 0.1.0a8` in the evaluation tables is a different
        # artifact and must not be read as `0.1.0`.
        for m in re.finditer(re.escape(crate) + r"[ -]+(\d+\.\d+\.\d+(?:-[\w.]+)?)(?![\w.])", outside):
            if m.group(1) != version:
                failures.append(f"README names {crate} {m.group(1)}; claims.json has {version}")
    stale = number_tokens(outside) & retired_values(claims)
    # Values that also appear in a current claim are not stale; a count like 1,000,000 is both.
    current = set()
    for c in claims["claims"].values():
        current |= number_tokens(fmt(c["value"], c["unit"]))
        current |= number_tokens(c["statement"]) | number_tokens(c["scope"])
    stale -= current
    for n in sorted(stale):
        failures.append(f"README quotes {n}, which belongs to a retired claim")
    for path in PROSE:
        with open(path, encoding="utf-8") as fh:
            prose = fh.read()
        for line, name in retired_names(prose):
            failures.append(f"{os.path.relpath(path, ROOT)} line {line} uses the retired product name {name!r}")
    if failures:
        print("claims-check: FAIL")
        for f in failures:
            print("  " + f)
        return 1
    print(f"claims-check: OK ({len(ROWS)} rows match claims.json; claims.json matches the reports)")
    # A claim that is not published as a plain value is named on every run, so its absence is never silent.
    for cid, c in claims["claims"].items():
        if c.get("status") in REMARKED:
            print(f"  {REMARKED[c['status']]}: {cid}: {c.get('note', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
