#!/usr/bin/env python3
"""Keep the benchmark numbers in README.md equal to claims.json.

README.md carries one block between `<!-- claims:start -->` and `<!-- claims:end -->` that is rendered
from claims.json by this script, so every number in it is a claim value, a claim's range, or the run
count. `make claims-check` re-renders the block and fails when the README differs from it; `--write`
replaces the block. The check also fails when the README names a vendored crate at a version other
than the one claims.json records, and when any line outside the block quotes a retired claim's value
(the numbers a reader is most likely to copy from an older README).

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


def render(claims: dict) -> str:
    meta, table = claims["meta"], claims["claims"]
    runs = meta.get("runs", [])
    versions = ", ".join(f"{c} {v}" for c, v in meta["crate_versions"].items())
    lines = [START]
    if runs:
        lines.append(
            f"Medians of {len(runs)} sequential runs of `scripts/run_once.sh` on {meta['hardware']}, "
            f"{meta['toolchain']}, `{meta['compiler_flags']}`, with synthetic data, at commit "
            f"`{meta['benchmark_commit']}` ({versions}). The range column is [min, max] across the runs; the raw "
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
        unit = "" if c["unit"] == "bool" else f" {c['unit']}"
        rng = "" if c["range"] is None else f"{fmt(c['range'][0], c['unit'])} to {fmt(c['range'][1], c['unit'])}"
        lines.append(
            f"| {crate} | {c['statement']} | {fmt(c['value'], c['unit'])}{unit} | {c['run_count']} | {rng} | `{program}` |"
        )
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
    if failures:
        print("claims-check: FAIL")
        for f in failures:
            print("  " + f)
        return 1
    print(f"claims-check: OK ({len(ROWS)} rows match claims.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
