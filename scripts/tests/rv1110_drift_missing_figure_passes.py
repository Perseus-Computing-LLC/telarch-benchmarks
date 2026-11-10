"""severity: medium
finding: drift-check exits 0 when a figure of record has no comparable value in claims.json (absent, null, text, NaN)

Location: scripts/drift_check.py:41-44 (a claim that is absent, or whose value is not a number, becomes the
verdict "missing-in-current" and is skipped), scripts/bench_figures.py:74 (FLAGGED holds only "regressed" and
"parameter-changed"), scripts/drift_check.py:96 (exit code from `flagged` alone); scripts/bench_figures.py:62-70
(a NaN median is neither inside the range nor "worse", so it comes out as "outside-range").

Effect: the gate "claims.json must not have regressed against the committed baseline" (Makefile:37) passes
vacuously. A claims.json with no claims at all, or one where a regressed figure was renamed, dropped, nulled or
turned into "n/a", exits 0 with "flagged: none". Dropping a figure is the cheapest way to hide its regression.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
FIGURES = {
    "harpe_small_p50_us": {"value": 2.0, "range": [1.9, 2.1], "unit": "µs", "run_count": 3, "higher_is_better": False},
    "argus_append_eps": {"value": 900000, "range": [880000, 920000], "unit": "events/s", "run_count": 3, "higher_is_better": True},
}


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dc():
    mod = _load("drift_check")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    return mod


def drift_exit(dc, tmp_path, claims_text):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"meta": {"release": "v0", "run_count": 3, "hardware": "h"}, "figures": FIGURES}), encoding="utf-8")
    cur = tmp_path / "claims.json"
    cur.write_text(claims_text, encoding="utf-8")
    return dc.main(["--baseline", str(base), "--current", str(cur)])


def current(**overrides):
    claims = {fid: {"value": f["value"], "range": f["range"], "unit": f["unit"], "run_count": 3} for fid, f in FIGURES.items()}
    for fid, value in overrides.items():
        if value is ...:
            del claims[fid]
        else:
            claims[fid].update(value=value, range=None)
    return json.dumps({"meta": {"hardware": "h"}, "claims": claims})


def test_control_unchanged_passes_and_a_slower_figure_fails(dc, tmp_path):
    assert drift_exit(dc, tmp_path, current()) == 0
    assert drift_exit(dc, tmp_path, current(harpe_small_p50_us=3.0)) == 1          # 50 % slower


@pytest.mark.parametrize("label, value", [("absent", ...), ("null", None), ("text", "n/a"), ("NaN", float("nan"))])
def test_a_figure_of_record_without_a_usable_current_value_fails_the_gate(dc, tmp_path, label, value):
    assert drift_exit(dc, tmp_path, current(harpe_small_p50_us=value)) != 0, (
        f"baseline has harpe_small_p50_us = 2.0 µs; claims.json has it {label}; drift-check exits 0")


def test_a_claims_json_with_no_claims_fails_the_gate(dc, tmp_path):
    assert drift_exit(dc, tmp_path, json.dumps({"meta": {}, "claims": {}})) != 0
