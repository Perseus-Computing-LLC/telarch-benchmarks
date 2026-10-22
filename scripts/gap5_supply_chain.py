#!/usr/bin/env python3
"""Gap 5: supply-chain baseline.

1. Runs `cargo audit` (RustSec advisory database) over Cargo.lock.
2. Writes a CycloneDX SBOM of Cargo.lock to sbom/.
3. Lists the supply-chain controls this checkout has, read from its files: workflows that run
   cargo audit, Dependabot and OpenSSF Scorecard configuration, artifact attestation, actions
   pinned to a full commit hash, SECURITY.md and LICENSE. Nothing is reported that the files do not show.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOST_CARGO_BIN = "/opt/data/home/.cargo/bin"


def run_cargo_audit() -> dict:
    env = os.environ.copy()
    if os.path.isdir(_HOST_CARGO_BIN):  # the maintainer's host; elsewhere (CI) cargo comes from PATH
        env["PATH"] = f"{_HOST_CARGO_BIN}:{env.get('PATH', '')}"
        env["HOME"] = "/opt/data/home"
    cargo_bin = shutil.which("cargo", path=env["PATH"]) or "cargo"
    res = subprocess.run([cargo_bin, "audit", "--json"], cwd=ROOT, capture_output=True, text=True, env=env)
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return {"audit_passed": res.returncode == 0, "vulnerabilities_found": None,
                "note": "cargo audit printed no JSON; audit_passed is its exit status"}
    vulns = data.get("vulnerabilities", {}).get("list", [])
    db = data.get("database", {})
    return {
        "audit_passed": not vulns,
        "vulnerabilities_found": len(vulns),
        "warnings_count": sum(len(w) for w in data.get("warnings", {}).values()),
        "advisory_database": {k.replace("-", "_"): db.get(k) for k in ("advisory-count", "last-commit", "last-updated")},
    }


def _suite_version() -> str:
    """The vendored crates are released separately, so name each one's version (from its Cargo.toml)."""
    versions = []
    for crate in ("harpe", "kibisis", "argus-ledger"):
        with open(os.path.join(ROOT, crate, "Cargo.toml")) as f:
            version = next((line.split("=", 1)[1].strip().strip('"') for line in f if line.startswith("version =")),
                           "unknown")
        versions.append(f"{crate} {version}")
    return ", ".join(versions)


def generate_cyclonedx_sbom() -> dict:
    """Generates CycloneDX SBOM representing the Telarch Suite workspace dependency tree."""
    lock_file = os.path.join(ROOT, "Cargo.lock")
    components = []

    if os.path.exists(lock_file):
        # Simple parser for Cargo.lock packages
        with open(lock_file, "r") as f:
            lines = f.readlines()
        cur_pkg = {}
        for line in lines:
            line = line.strip()
            if line == "[[package]]":
                if cur_pkg and "name" in cur_pkg:
                    components.append({
                        "type": "library",
                        "name": cur_pkg["name"],
                        "version": cur_pkg.get("version", "unknown"),
                        "purl": f"pkg:cargo/{cur_pkg['name']}@{cur_pkg.get('version', '')}",
                    })
                cur_pkg = {}
            elif line.startswith("name ="):
                cur_pkg["name"] = line.split("=")[1].strip().strip('"')
            elif line.startswith("version ="):
                cur_pkg["version"] = line.split("=")[1].strip().strip('"')
        if cur_pkg and "name" in cur_pkg:
            components.append({
                "type": "library",
                "name": cur_pkg["name"],
                "version": cur_pkg.get("version", "unknown"),
                "purl": f"pkg:cargo/{cur_pkg['name']}@{cur_pkg.get('version', '')}",
            })

    sbom = {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tools": [{"vendor": "Perseus Computing LLC", "name": "telarch-sbom-generator", "version": "0.1.0"}],
            "component": {
                "type": "framework",
                "name": "telarch-suite",
                "version": _suite_version(),
                "description": "Telarch cognitive infrastructure co-processors (Harpe, Kibisis, Argus)",
            },
        },
        "components": components,
    }

    sbom_dir = os.path.join(ROOT, "sbom")
    os.makedirs(sbom_dir, exist_ok=True)
    sbom_path = os.path.join(sbom_dir, "telarch-suite-sbom.json")
    with open(sbom_path, "w") as f:
        json.dump(sbom, f, indent=2)

    return {
        "sbom_format": "CycloneDX v1.5",
        "sbom_file": "sbom/telarch-suite-sbom.json",
        "total_components_cataloged": len(components),
    }


def governance_controls(root: str = ROOT) -> dict:
    """The supply-chain controls this checkout shows, read from its files rather than asserted."""
    flows = {}
    for path in sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml"))):
        with open(path, encoding="utf-8") as f:
            flows[os.path.basename(path)] = f.read()
    uses = [u for text in flows.values() for u in re.findall(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", text, re.M)
            if not u.startswith("./")]
    pinned = lambda u: re.search(r"@[0-9a-f]{40}$", u) is not None
    having = lambda needle: [name for name, text in flows.items() if needle in text]
    found = lambda *paths: next((p for p in paths if os.path.exists(os.path.join(root, p))), None)
    license_line = None
    if found("LICENSE"):
        with open(os.path.join(root, "LICENSE"), encoding="utf-8") as f:
            license_line = f.readline().strip()
    return {
        "workflows": sorted(flows),
        "cargo_audit_workflows": having("cargo audit"),
        "dependabot_config": found(".github/dependabot.yml", ".github/dependabot.yaml"),
        "openssf_scorecard_workflows": having("ossf/scorecard-action"),
        "artifact_attestation_workflows": having("actions/attest"),
        "actions_pinned_to_full_commit_hash": f"{sum(map(pinned, uses))} of {len(uses)}",
        "actions_not_pinned": sorted({u for u in uses if not pinned(u)}),
        "security_policy": found("SECURITY.md", ".github/SECURITY.md"),
        "license": license_line,
    }


def main():
    print("=" * 80)
    print("      TELARCH GAP 5: SUPPLY-CHAIN BASELINE AUDIT BENCHMARK      ")
    print("        Linux x86_64, Intel Core i5-13500 (20 threads)          ")
    print("=" * 80)

    # 1. Cargo Audit
    print("\n[1/3] Running Cargo Audit (RustSec Advisory Database Scan):")
    audit_res = run_cargo_audit()
    print(f"  • Vulnerabilities Found:       {audit_res['vulnerabilities_found']}")
    print(f"  • Audit Status:                {'PASSED (0 VULNERABILITIES)' if audit_res['audit_passed'] else 'FAILED'}")

    # 2. CycloneDX SBOM Generation
    print("\n[2/3] Generating CycloneDX v1.5 Software Bill of Materials (SBOM):")
    sbom_res = generate_cyclonedx_sbom()
    print(f"  • Format:                      {sbom_res['sbom_format']}")
    print(f"  • Components Cataloged:        {sbom_res['total_components_cataloged']} packages")
    print(f"  • SBOM File Saved:             {sbom_res['sbom_file']}")

    # 3. Supply-chain controls, as the checkout shows them
    print("\n[3/3] Supply-chain controls found in this checkout:")
    governance = governance_controls()
    for k, v in governance.items():
        print(f"  • {k.replace('_', ' ')}: {(', '.join(v) if isinstance(v, list) else v) or 'none'}")

    report = {
        "benchmark": "telarch-gap5-supply-chain",
        "suite": "Telarch Suite (TCI)",
        "host": "Linux x86_64, Intel Core i5-13500 (20 threads)",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cargo_audit": audit_res,
        "cyclonedx_sbom": sbom_res,
        "governance_controls": governance,
    }

    out_file = os.path.join(ROOT, "reports", "gap5_supply_chain.json")
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[REPORT] Saved machine-readable report to: {out_file}")


if __name__ == "__main__":
    main()
