"""severity: medium
finding: claims.json attributes the committed runs' medians to the machine and moment build_claims.py runs on

Location: scripts/build_claims.py:110 and :798 (the hardware line is bench_env.hardware() of the machine running
build_claims.py, in build() and again in aggregate()), :678-684 (generated, toolchain, benchmark_commit likewise),
:810-814 (main: when reports/runs/<date>/ exists every numeric value is the median of those runs). Every report
carries its own `host` and `timestamp_utc`; build_claims.py reads neither. Makefile:54-72 (`make bench` ends with
build_claims.py), README.md:136-143 ("Run everything").

Effect: reports/runs/2026-11-05/ is committed, so `make bench` on any clone, on any machine, writes a claims.json
whose numbers are the development host's committed medians while `hardware`, `toolchain`, `benchmark_commit` and
`generated` describe the machine that just ran it; the run it just made is used only for text and pass/fail
values. Nothing compares the `host` the reports recorded with the hardware line they are published under.
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNS = "reports/runs/2026-11-05"
MEASURED_ON = "Linux aarch64, Measuring Host CPU, 64 logical CPUs"
BUILT_ON = "Linux x86_64, Building Host CPU, 8 logical CPUs"
CID = "harpe_small_p50_us"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def built(tmp_path, monkeypatch):
    """`build_claims.py` run on BUILT_ON over a tree whose three run directories were measured on MEASURED_ON."""
    bc = _load("build_claims")
    assert Path(bc.__file__).resolve().parent == SCRIPTS
    root = tmp_path / "repo"
    shutil.copytree(REPO / "reports", root / "reports", ignore=shutil.ignore_patterns("raw_csvs", "bisect"))
    shutil.copy2(REPO / "claims_schema.json", root / "claims_schema.json")
    for crate in ("harpe", "kibisis", "argus-ledger"):
        (root / crate).mkdir()
        shutil.copy2(REPO / crate / "Cargo.toml", root / crate / "Cargo.toml")
    for path in sorted((root / RUNS).glob("run*/*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if "host" in report:
            report["host"] = MEASURED_ON
            path.write_text(json.dumps(report), encoding="utf-8")
    fresh = root / "reports" / "microbenchmarks_phase1.json"          # what this machine's own `make bench` wrote
    report = json.loads(fresh.read_text(encoding="utf-8"))
    report["host"] = BUILT_ON
    report["harpe"]["small_context"]["latency_p50_us"] = 7.777
    fresh.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(bc, "ROOT", str(root))
    monkeypatch.setattr(bc.bench_env, "hardware", lambda: BUILT_ON)
    monkeypatch.setattr(bc.bench_env, "commit", lambda full=False: "b" * (40 if full else 7))
    monkeypatch.delenv("TELARCH_RUNS_DATE", raising=False)
    try:
        bc.main()
    except (AssertionError, ValueError, SystemExit) as e:
        return root, None, e
    return root, json.loads((root / "claims.json").read_text(encoding="utf-8")), None


def test_control_the_published_value_is_the_median_of_the_run_directories(built):
    root, claims, refused = built
    assert refused is None
    hosts = {json.loads((root / RUNS / r / "microbenchmarks_phase1.json").read_text())["host"] for r in ("run1", "run2", "run3")}
    assert hosts == {MEASURED_ON}
    assert claims["meta"]["runs"] == [f"{RUNS}/run{i}" for i in (1, 2, 3)]
    c = claims["claims"][CID]
    assert (c["value"], c["run_count"]) == (2.026, 3)          # the runs' median; this machine's 7.777 is not used


def test_the_hardware_line_names_the_machine_the_published_runs_were_measured_on(built):
    root, claims, refused = built
    if refused is not None:
        return                                                 # refusing to mix hosts is correct too
    c = claims["claims"][CID]
    assert "Measuring Host CPU" in c["hardware"] and "Measuring Host CPU" in claims["meta"]["hardware"], (
        f"{CID}={c['value']} is the median of three reports that say host={MEASURED_ON!r}; claims.json publishes it "
        f"as measured on {c['hardware']!r} at commit {c['commit']!r}")
