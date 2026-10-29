"""B-B acceptance (roadmap, telarch-benchmarks #24): per-commit attribution.

bisect_bench.compare() turns the per-run figures of two refs into per-figure medians, ranges, a noise
floor and a drift verdict; drift_check.check() applies the same rule to a committed baseline against
claims.json. Both are pure functions so CI and the harness share one definition of "regressed".
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bb():
    return _load("bisect_bench")


@pytest.fixture(scope="module")
def dc():
    return _load("drift_check")


def runs(**figures):
    """[{id: value}, ...] — one dict per run, from parallel value lists."""
    n = len(next(iter(figures.values())))
    return [{k: v[i] for k, v in figures.items()} for i in range(n)]


def test_abba_order_interleaves_and_repeats(bb):
    assert bb.abba_order(1) == ["A", "B", "B", "A"]
    assert bb.abba_order(3) == ["A", "B", "B", "A"] * 3
    with pytest.raises(ValueError):
        bb.abba_order(0)


def test_compare_reports_median_range_noise_and_delta(bb):
    a = runs(x=[100.0, 104.0, 96.0, 102.0], y=[10, 10, 10, 10])
    b = runs(x=[101.0, 99.0, 103.0, 100.0], y=[10, 10, 10, 10])
    r = bb.compare(a, b, threshold_pct=10.0, higher_is_better={"x": False, "y": False})
    fx = r["figures"]["x"]
    assert fx["a"]["median"] == 101.0 and fx["a"]["range"] == [96.0, 104.0]
    assert fx["b"]["median"] == 100.5
    assert fx["noise_floor_pct"] == pytest.approx((104.0 - 96.0) / 101.0 * 100, rel=1e-6)
    assert fx["delta_pct"] == pytest.approx((100.5 - 101.0) / 101.0 * 100, rel=1e-6)
    assert fx["verdict"] == "within-noise"
    assert r["figures"]["y"]["verdict"] == "within-noise" and r["flagged"] == []


def test_drift_is_beyond_range_by_more_than_threshold(bb):
    # latency figure (lower is better): B 25 % slower, far outside A's spread -> drift
    a = runs(lat=[100.0, 102.0, 98.0, 101.0], tput=[1000, 1010, 990, 1005])
    b = runs(lat=[125.0, 126.0, 124.0, 125.5], tput=[1002, 995, 1008, 1000])
    r = bb.compare(a, b, threshold_pct=10.0, higher_is_better={"lat": False, "tput": True})
    assert r["figures"]["lat"]["verdict"] == "regressed"
    assert r["flagged"] == ["lat"]
    # 25 % faster is an improvement, not a regression, and is reported as such
    b2 = runs(lat=[75.0, 76.0, 74.0, 75.5], tput=[1002, 995, 1008, 1000])
    assert bb.compare(a, b2, threshold_pct=10.0, higher_is_better={"lat": False, "tput": True})["figures"]["lat"]["verdict"] == "improved"
    # just outside the range but under the threshold -> "outside-range", not flagged
    b3 = runs(lat=[104.0, 105.0, 103.0, 104.5], tput=[1002, 995, 1008, 1000])
    r3 = bb.compare(a, b3, threshold_pct=10.0, higher_is_better={"lat": False, "tput": True})
    assert r3["figures"]["lat"]["verdict"] == "outside-range" and r3["flagged"] == []


def test_missing_figure_in_one_ref_is_reported_not_crashed(bb):
    a = runs(x=[1.0, 1.0, 1.0, 1.0], old=[5, 5, 5, 5])
    b = runs(x=[1.0, 1.0, 1.0, 1.0], new=[7, 7, 7, 7])
    r = bb.compare(a, b, threshold_pct=10.0, higher_is_better={"x": False})
    assert r["figures"]["old"]["verdict"] == "missing-in-b" and r["figures"]["new"]["verdict"] == "missing-in-a"
    assert r["flagged"] == []


def test_workload_parameter_change_is_flagged(bb):
    a = runs(harpe_curve_turns=[100000] * 4, harpe_curve_avg_us=[26.0, 26.1, 25.9, 26.0])
    b = runs(harpe_curve_turns=[50000] * 4, harpe_curve_avg_us=[26.0, 26.1, 25.9, 26.0])
    r = bb.compare(a, b, threshold_pct=10.0)
    assert r["figures"]["harpe_curve_turns"]["verdict"] == "parameter-changed" and r["flagged"] == ["harpe_curve_turns"]


def test_drift_check_against_committed_baseline(dc, tmp_path):
    baseline = {"meta": {"release": "v0.1.0-alpha.9"},
                "figures": {"harpe_small_p50_us": {"value": 1.82, "range": [1.80, 1.85], "unit": "us", "higher_is_better": False},
                            "argus_append_eps": {"value": 1_000_000, "range": [980_000, 1_020_000], "unit": "events/s", "higher_is_better": True},
                            "kib_split_brain": {"value": 0, "range": [0, 0], "unit": "count", "higher_is_better": False}}}
    current = {"claims": {"harpe_small_p50_us": {"value": 2.30, "range": [2.28, 2.33]},     # 26 % slower -> flag
                          "argus_append_eps": {"value": 1_005_000, "range": [990_000, 1_015_000]},
                          "kib_split_brain": {"value": 0, "range": [0, 0]}}}
    (tmp_path / "b.json").write_text(json.dumps(baseline)); (tmp_path / "c.json").write_text(json.dumps(current))
    r = dc.check(tmp_path / "b.json", tmp_path / "c.json", threshold_pct=10.0)
    assert r["flagged"] == ["harpe_small_p50_us"]
    assert r["figures"]["argus_append_eps"]["verdict"] == "within-noise"
    assert r["figures"]["kib_split_brain"]["verdict"] == "within-noise"     # zero baseline must not divide by zero
    assert dc.main(["--baseline", str(tmp_path / "b.json"), "--current", str(tmp_path / "c.json")]) == 1
    current["claims"]["harpe_small_p50_us"] = {"value": 1.84, "range": [1.81, 1.86]}
    (tmp_path / "c.json").write_text(json.dumps(current))
    assert dc.main(["--baseline", str(tmp_path / "b.json"), "--current", str(tmp_path / "c.json")]) == 0


def test_committed_alpha9_baseline_is_well_formed_and_from_claims(dc):
    p = SCRIPTS.parent / "baselines" / "v0.1.0-alpha.9.json"
    assert p.exists(), "baselines/v0.1.0-alpha.9.json must be committed"
    b = json.loads(p.read_text())
    assert b["meta"]["release"] == "v0.1.0-alpha.9" and b["meta"]["run_count"] >= 3 and "hardware" in b["meta"]
    assert len(b["figures"]) >= 30
    for fid, f in b["figures"].items():
        assert isinstance(f["value"], (int, float)) and len(f["range"]) == 2 and f["range"][0] <= f["value"] <= f["range"][1], fid
        assert f["higher_is_better"] in (True, False, None), fid    # None = workload parameter, must not change
    # the baseline must agree with claims.json at the figures of record
    claims = json.loads((SCRIPTS.parent / "claims.json").read_text())["claims"]
    for fid, f in b["figures"].items():
        assert claims[fid]["value"] == f["value"], fid


def test_scratch_dir_is_private_to_the_run(tmp_path, monkeypatch):
    """Two batteries running at once used to share $TMPDIR/telarch-bisect/bisect-<sha> and force-remove it on
    start, so one run's cargo build lost its target dir under the other ('could not write output to .../deps').
    Each run must get a scratch directory no other run can name."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bb = _load("bisect_bench")
    a = bb.scratch_dir()
    b = bb.scratch_dir()
    assert a != b
    assert a.is_dir() and b.is_dir()
    assert a.parent == b.parent == tmp_path / "telarch-bisect"
    assert all(ch.isalnum() or ch in "-_" for ch in a.name)
