"""severity: medium
finding: bisect-bench exits 0 with "flagged: none" when one ref produced no usable figures at all

Location: scripts/bisect_bench.py:150-154 (figures_of turns any FileNotFoundError, KeyError or TypeError from
build_claims.build() into an empty dict and a stderr warning, so one absent report or one renamed key discards
every figure of that run), :165-168 (compare records "missing-in-a"/"missing-in-b" and moves on), :250 (the exit
status looks at `flagged` only); scripts/bench_figures.py:74.

Effect: `make bisect-bench REF_A=v0.1.0-alpha.9 REF_B=HEAD` answers "no regression" when it compared nothing.
build_claims.build() is the *current* checkout's reader applied to each ref's reports, so an older ref whose
report lacks one key the current reader indexes loses all of its figures in every run, and the Makefile target
succeeds. The docstring (lines 27-28) says such a ref "has that figure recorded as missing"; in fact the whole
arm is, and nothing fails.

The test drives the real main() with checkout, cargo build and the program runs replaced by copies of the
committed reports, so no benchmark runs.
"""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bisect(tmp_path, monkeypatch):
    """main() with both refs 'measured' by a function that writes what `arm_b` says ref B's programs wrote."""
    bb = _load("bisect_bench")
    assert Path(bb.__file__).resolve().parent == SCRIPTS
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(bb, "resolve", lambda ref: {"REF_A": "aaaaaaa", "REF_B": "bbbbbbb"}[ref])
    monkeypatch.setattr(bb, "checkout", lambda ref, sha, scratch: scratch / f"bisect-{sha}")
    monkeypatch.setattr(bb, "build", lambda wt, log: 0.0)

    def run(arm_b):
        def fake_run_once(wt, dest, pin):
            dest.mkdir(parents=True, exist_ok=True)
            for name in bb.build_claims.REPORT_FILES:
                report = json.loads((REPO / "reports" / name).read_text(encoding="utf-8"))
                if dest.name.endswith("-B"):
                    report = arm_b(name, report)
                if report is not None:
                    (dest / name).write_text(json.dumps(report), encoding="utf-8")
            return 1.0

        monkeypatch.setattr(bb, "run_once", fake_run_once)
        out = tmp_path / f"out{len(list(tmp_path.glob('out*')))}"
        rc = bb.main(["REF_A", "REF_B", "--rounds", "1", "--no-warmup", "--keep-worktrees", "--out", str(out)])
        return rc, json.loads((out / "report.json").read_text(encoding="utf-8"))

    yield run
    shutil.rmtree(tmp_path / "telarch-bisect", ignore_errors=True)


def slower(name, report):
    if name == "microbenchmarks_phase1.json":
        report["harpe"]["small_context"]["latency_p50_us"] *= 2
    return report


def without_one_key(name, report):
    if name == "microbenchmarks_phase1.json":
        del report["harpe"]["small_context"]["budget_tokens"]      # used only for a claim's scope text
    return report


def test_control_identical_refs_pass_and_a_slower_ref_is_flagged(bisect):
    rc, report = bisect(lambda name, r: r)
    assert rc == 0 and report["flagged"] == []
    assert report["figures"]["harpe_small_p50_us"]["b"]["n"] == 2
    rc, report = bisect(slower)
    assert rc == 1 and report["flagged"] == ["harpe_small_p50_us"]


@pytest.mark.parametrize("arm_b", [lambda name, r: None, lambda name, r: None if name == "swarm_partition.json" else r, without_one_key],
                         ids=["no reports written", "one report of eight missing", "one key of one report missing"])
def test_a_ref_that_yielded_no_figures_is_not_reported_as_no_regression(bisect, arm_b):
    rc, report = bisect(arm_b)
    usable = [fid for fid, f in report["figures"].items() if not f["verdict"].startswith("missing")]
    assert rc != 0, (
        f"ref B contributed no figures ({len(report['figures'])} figures, {len(usable)} comparable); "
        f"bisect-bench printed flagged: none and exited {rc}")
