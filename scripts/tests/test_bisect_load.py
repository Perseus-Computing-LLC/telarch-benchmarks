"""P12.0(b): bisect_bench.py records /proc/loadavg at the start and end of every timed run, warm-ups included, into
report.json as meta.load, keyed by run (warmup-A, warmup-B, run01-A, ...).

A reading is the 1/5/15-minute load averages x100 as ints and the running/total scheduling entities, with the UTC
second it was taken; an unreadable source is recorded as an error, not raised. The readings sit in meta beside the
figures, never among them, so compare() never flags them. Every test here reads a fake loadavg file.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import bench_env  # noqa: E402

FIELDS = {"at", "load1_x100", "load5_x100", "load15_x100", "running", "total"}


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bb():
    return _load("bisect_bench")


@pytest.mark.parametrize("content, want", [
    ("2.39 1.07 12.45 3/1234 56789\n", (239, 107, 1245, 3, 1234)),
    ("0.00 0.01 0.10 1/99 5\n", (0, 1, 10, 1, 99)),
])
def test_a_reading_is_load_x100_as_ints_and_running_total(tmp_path, content, want):
    src = tmp_path / "loadavg"
    src.write_text(content)
    r = bench_env.loadavg(src)
    assert set(r) == FIELDS
    assert (r["load1_x100"], r["load5_x100"], r["load15_x100"], r["running"], r["total"]) == want
    assert all(type(r[k]) is int for k in FIELDS - {"at"}) and isinstance(r["at"], str)


@pytest.mark.parametrize("content", [None, "", "abc 1.00 2.00 3/4 5\n", "1.00 2.00 3.00 34 5\n"])
def test_an_unreadable_source_is_recorded_not_raised(tmp_path, content):
    src = tmp_path / "loadavg"
    if content is not None:
        src.write_text(content)
    r = bench_env.loadavg(src)
    assert set(r) == {"at", "error"} and r["error"]


def test_every_timed_run_carries_a_reading_at_its_start_and_end(bb, tmp_path, monkeypatch):
    assert bb.LOADAVG == Path("/proc/loadavg")
    src = tmp_path / "loadavg"
    src.write_text("1.00 1.00 1.00 1/100 7\n")
    monkeypatch.setattr(bb, "LOADAVG", src)
    calls = []

    def fake_run_once(wt, dest, pin):
        calls.append(dest.name)
        dest.mkdir(parents=True, exist_ok=True)
        src.write_text(f"{len(calls)}.00 1.00 1.00 {len(calls)}/100 7\n")  # the host's load moves during each run
        return 1.0

    monkeypatch.setattr(bb, "resolve", lambda ref: {"refA": "a" * 7, "refB": "b" * 7}[ref])
    monkeypatch.setattr(bb, "checkout", lambda ref, sha, scratch: tmp_path / "wt" / sha)
    monkeypatch.setattr(bb, "build", lambda wt, log: 0.0)
    monkeypatch.setattr(bb, "run_once", fake_run_once)
    monkeypatch.setattr(bb, "figures_of", lambda d: {})
    monkeypatch.setattr(bb, "scratch_dir", lambda: tmp_path / "scratch")
    monkeypatch.setattr(bb, "pin_prefix", lambda: [])
    monkeypatch.setattr(bb.bench_env, "hardware", lambda: "test")
    monkeypatch.setattr(bb.bench_env, "toolchain", lambda: "test")
    out = tmp_path / "out"
    bb.main(["refA", "refB", "--rounds", "1", "--out", str(out), "--keep-worktrees"])

    rep = json.loads((out / "report.json").read_text())
    load = rep["meta"]["load"]
    assert calls == ["warmup-A", "warmup-B", "run01-A", "run02-B", "run03-B", "run04-A"]
    assert list(load) == calls
    for i, name in enumerate(calls, 1):
        start, end = load[name]["start"], load[name]["end"]
        assert set(start) == FIELDS and set(end) == FIELDS
        assert (start["load1_x100"], start["running"]) == (100 * (i - 1) if i > 1 else 100, max(i - 1, 1))
        assert (end["load1_x100"], end["running"]) == (100 * i, i)
    assert rep["flagged"] == [] and "load" not in rep["figures"]
