"""severity: medium
finding: a resumed run keeps the first process's commit and library versions in its manifest, whatever code produces the remaining rows

Location: evals/telarch_evals/runlog.py:72-81 (on resume the stored manifest is loaded and only `config` is
compared with the current settings), :87-93 (repo_commit, packages and package_sources are written once, when the
run is created).

Effect: package_sources exists "so that a run names the exact code it measured" (memory.py:84-85) and
evals/README.md:101,154 say the manifest holds the versions of Kibisis, Harpe and Argus and the commit of this
repository. After `--resume` under another kibisis or harpe release, or another commit of the harness, the rows
written from then on were retrieved and scored by code the manifest does not name: one accuracy, two library
versions, the older one published. Nothing is refused and nothing is recorded; the ledger's eval.run.resumed
event carries only the manifest's hash.
"""
import json
from pathlib import Path

import pytest
import telarch_evals
from telarch_evals import runlog
from telarch_evals.runlog import RunLog

EVALS = Path(__file__).resolve().parents[1]
CONFIG = {"system": "telarch", "benchmark": "longmemeval"}


def environment(monkeypatch, kibisis: str, commit: str):
    monkeypatch.setattr(runlog, "package_versions", lambda: {"kibisis": kibisis, "harpe": "0.1.0a12"})
    monkeypatch.setattr(runlog, "package_sources", lambda: {"kibisis": {"version": kibisis, "source": "index"}})
    monkeypatch.setattr(runlog, "repo_commit", lambda: commit)


def first_process(tmp_path, monkeypatch):
    environment(monkeypatch, "0.1.0a15", "1111111111111111111111111111111111111111")
    log = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[])
    log.write({"id": "q1", "status": "ok"})
    log.write({"id": "q2", "status": "error", "error": "provider down"})
    log.finish({"rows": 1, "errors": 1, "complete": False})
    return log.dir


def test_control_the_manifest_names_the_code_of_the_first_process_and_a_resume_checks_the_settings(tmp_path, monkeypatch):
    assert Path(telarch_evals.__file__).resolve().is_relative_to(EVALS)
    run_dir = first_process(tmp_path, monkeypatch)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["packages"]["kibisis"] == "0.1.0a15" and manifest["repo_commit"].startswith("1111111")
    with pytest.raises(SystemExit):
        RunLog(tmp_path / "runs", "longmemeval", {**CONFIG, "system": "bm25"}, sources=[], resume=run_dir)
    same = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[], resume=run_dir)
    assert same.done == {"q1"}


@pytest.mark.parametrize("kibisis, commit, changed", [
    ("0.1.0a16", "1111111111111111111111111111111111111111", "0.1.0a16"),
    ("0.1.0a15", "2222222222222222222222222222222222222222", "2222222222222222222222222222222222222222"),
], ids=["another kibisis release", "another commit of the harness"])
def test_a_resume_under_other_code_is_refused_or_recorded(tmp_path, monkeypatch, kibisis, commit, changed):
    run_dir = first_process(tmp_path, monkeypatch)
    environment(monkeypatch, kibisis, commit)
    try:
        resumed = RunLog(tmp_path / "runs", "longmemeval", CONFIG, sources=[], resume=run_dir)
    except SystemExit:
        return                                                    # refusing is correct
    resumed.write({"id": "q2", "status": "ok"})
    resumed.finish({"rows": 2, "errors": 0, "complete": True})
    ledger = (tmp_path / "runs" / "ledger.jsonl").read_text()
    assert changed in (run_dir / "manifest.json").read_text() or changed in ledger, (
        f"row q2 was produced under {changed}; neither manifest.json nor the run ledger mentions it, and the run is complete")
