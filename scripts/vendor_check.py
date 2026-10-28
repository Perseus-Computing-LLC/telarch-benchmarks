#!/usr/bin/env python3
"""Fail when a vendored crate's Cargo.toml version differs from the version claims.json names.

claims.json carries meta.crate_versions and every claim repeats it. The vendored copies under harpe/,
kibisis/ and argus-ledger/ are what the programs build, so if either side moves without the other the
published numbers describe code that is not in the repository. Run as `make vendor-check`; CI runs it too.
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATES = ("harpe", "kibisis", "argus-ledger")


def cargo_version(crate: str) -> str:
    path = os.path.join(ROOT, crate, "Cargo.toml")
    with open(path, encoding="utf-8") as fh:
        in_package = False
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("["):
                in_package = stripped == "[package]"
                continue
            if in_package:
                m = re.match(r'version\s*=\s*"([^"]+)"', stripped)
                if m:
                    return m.group(1)
    raise SystemExit(f"{path}: no [package] version")


def run_record_versions(run: str) -> dict:
    """Crate versions a run's run_env.txt records (`crate_versions=harpe X, kibisis Y, argus-ledger Z`)."""
    path = os.path.join(ROOT, run, "run_env.txt")
    if not run or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if sep and key == "crate_versions":
                return dict(item.strip().rsplit(" ", 1) for item in value.split(",") if item.strip())
    return {}


def main() -> int:
    with open(os.path.join(ROOT, "claims.json"), encoding="utf-8") as fh:
        claims = json.load(fh)
    named = claims["meta"]["crate_versions"]
    failures = []
    for crate in CRATES:
        vendored = cargo_version(crate)
        claimed = named.get(crate)
        if vendored != claimed:
            failures.append(f"{crate}: Cargo.toml says {vendored}, claims.json meta says {claimed}")
        # Every claim repeats the versions; a stale one means claims.json was hand-edited. A claim carried
        # over from an earlier release names the versions its own run record names instead, and the
        # record must exist and agree: a figure may lag the vendored code, but not silently.
        for cid, claim in claims["claims"].items():
            got = claim.get("crate_versions", {}).get(crate)
            carried = claim.get("carried_over")
            if carried:
                recorded = run_record_versions(carried.get("run", ""))
                if recorded.get(crate) != got or got is None:
                    failures.append(
                        f"{crate}: carried-over claim {cid} names {got}, but {carried.get('run')}/run_env.txt "
                        f"records {recorded.get(crate)}"
                    )
            elif got != vendored:
                failures.append(f"{crate}: claim {cid} names {got}")
    if failures:
        print("vendor-check: FAIL")
        for line in failures:
            print("  " + line)
        return 1
    print("vendor-check: OK " + ", ".join(f"{c} {named[c]}" for c in CRATES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
