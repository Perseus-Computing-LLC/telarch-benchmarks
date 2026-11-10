"""severity: high
finding: aggregate() publishes a pass/fail claim from reports/ with run_count N even when all N runs say the opposite

Location: scripts/build_claims.py:773-785 (aggregate: only numeric claims are replaced by the runs' values; for a
bool or string the value built from the top-level reports/ directory is kept, and a note is written only when the
runs disagree among themselves), :811-814 (main builds the output from reports/ and never checks that reports/
equals any run directory).

Effect: reports/swarm_partition.json (or gap6_argus_anchored_checkpoints.json) can say "converged: true" while
run1..runN all say false; claims.json then carries value true, run_count N and an empty note, and README.md
prints "yes | N". The value does not come from the runs the claim counts.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bc():
    mod = _load("build_claims")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    return mod


def claim(value):
    return {"value": value, "run_count": 1, "median": None, "range": None, "note": "", "hardware": "h"}


def output(**claims):
    return {"meta": {"hardware": "h", "note": ""}, "claims": {k: claim(v) for k, v in claims.items()}}


def run_dirs(tmp_path, n=3):
    dirs = [tmp_path / f"run{i}" for i in range(1, n + 1)]
    for d in dirs:
        d.mkdir()
    return [str(d) for d in dirs]


def test_control_numeric_claims_take_the_median_of_the_runs(bc, tmp_path):
    out = output(harpe_small_p50_us=9.9, swarm_0pct_converged=True)
    per_run = [{"harpe_small_p50_us": claim(v), "swarm_0pct_converged": claim(True)} for v in (1.0, 3.0, 2.0)]
    bc.aggregate(out, per_run, run_dirs(tmp_path))
    c = out["claims"]["harpe_small_p50_us"]
    assert (c["value"], c["range"], c["run_count"]) == (2.0, [1.0, 3.0], 3)   # 9.9 from reports/ is replaced
    assert out["claims"]["swarm_0pct_converged"]["value"] is True and out["claims"]["swarm_0pct_converged"]["note"] == ""


def test_a_bool_claim_is_what_the_runs_say_not_what_reports_dir_says(bc, tmp_path):
    out = output(swarm_0pct_converged=True)                                   # reports/ says the swarm converged
    per_run = [{"swarm_0pct_converged": claim(False)} for _ in range(3)]      # every counted run says it did not
    try:
        bc.aggregate(out, per_run, run_dirs(tmp_path))
    except (AssertionError, ValueError, SystemExit):
        return                                                                # refusing to build is correct too
    c = out["claims"]["swarm_0pct_converged"]
    assert c["value"] is False, (
        f"3 of 3 runs report False; claims.json would publish value={c['value']!r}, run_count={c['run_count']}, "
        f"note={c['note']!r}")
