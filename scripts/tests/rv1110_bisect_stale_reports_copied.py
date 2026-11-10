"""severity: medium
finding: bisect-bench copies reports the run did not write: the ref's committed reports, or the previous run's

Location: scripts/bisect_bench.py:117-131 (run_once creates reports/ with exist_ok, runs the programs and copies
every REPORT_FILES name that exists under <worktree>/reports/ to the run's directory; nothing is removed before
the programs start and nothing checks that a file is newer than the run).

Effect: reports/*.json are tracked files (`git ls-files reports`), so a fresh `git worktree add` of any ref already
contains that ref's published reports. A program that exits 0 without writing its report (an older ref's binary
that does not know `--json <path>`, a script that writes elsewhere) leaves the committed file in place, and
bisect-bench scores it as this run's measurement: a figure measured weeks ago on the release run, identical in
every run of that arm. The same happens from run to run inside one worktree. The docstring's promise (lines 27-28),
"A ref whose programs do not write a report that build_claims expects has that figure recorded as missing, not
guessed", does not hold.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
NAME = "microbenchmarks_phase1.json"
WRITES = "import json, sys; json.dump({'written_by': sys.argv[1]}, open('reports/" + NAME + "', 'w'))"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bb():
    mod = _load("bisect_bench")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    assert NAME in mod.build_claims.REPORT_FILES
    return mod


def worktree(tmp_path):
    """What `git worktree add` leaves: the ref's committed report is already there."""
    wt = tmp_path / "bisect-abc1234"
    (wt / "reports").mkdir(parents=True)
    (wt / "reports" / NAME).write_text(json.dumps({"written_by": "the release run, committed with the ref"}), encoding="utf-8")
    return wt


def test_control_a_report_the_program_writes_is_the_one_copied(bb, tmp_path, monkeypatch):
    wt = worktree(tmp_path)
    monkeypatch.setattr(bb, "PROGRAMS", [[sys.executable, "-c", WRITES, "this run"]])
    bb.run_once(wt, tmp_path / "run01-A", [])
    assert json.loads((tmp_path / "run01-A" / NAME).read_text())["written_by"] == "this run"


def test_a_report_the_run_did_not_write_is_not_copied_from_the_checkout(bb, tmp_path, monkeypatch):
    wt = worktree(tmp_path)
    monkeypatch.setattr(bb, "PROGRAMS", [[sys.executable, "-c", "pass"]])        # exits 0, writes nothing
    bb.run_once(wt, tmp_path / "run01-A", [])
    copied = tmp_path / "run01-A" / NAME
    assert not copied.exists(), f"run01-A/{NAME} holds {copied.read_text()!r}, which no program of this run wrote"


def test_a_report_the_run_did_not_write_is_not_copied_from_the_previous_run(bb, tmp_path, monkeypatch):
    wt = worktree(tmp_path)
    monkeypatch.setattr(bb, "PROGRAMS", [[sys.executable, "-c", WRITES, "run 1"]])
    bb.run_once(wt, tmp_path / "run01-A", [])
    monkeypatch.setattr(bb, "PROGRAMS", [[sys.executable, "-c", "pass"]])
    bb.run_once(wt, tmp_path / "run04-A", [])
    copied = tmp_path / "run04-A" / NAME
    assert not copied.exists(), f"run04-A/{NAME} holds {copied.read_text()!r}: run 1's report scored again as run 4"
