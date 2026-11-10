"""severity: medium
finding: the 1M-event figure loses its carried-over provenance as soon as a newer runs date exists

Location: scripts/build_claims.py:714-726 (one_m_provenance looks for file_ledger_1m/run_env.txt only under the
latest date of reports/runs/, or TELARCH_RUNS_DATE, and returns {} when it is not there), :397-416 (with {} the
claim argus_file_append_1m_eps keeps the commit, hardware line and crate versions of the current build and gets no
`carried_over` entry), scripts/vendor_check.py:59-73 (which then sees current versions and passes).

Effect: the comment at build_claims.py:371-375 and vendor_check.py:59-61 state the rule: a figure may lag the
vendored code, but not silently. The single 1M-event run was measured on 2026-10-28 at commit ee741e8 against
argus-ledger 0.1.0-alpha.7; its record is found today only because the directory was copied by hand into
reports/runs/2026-11-05/. The next `scripts/run_once.sh <new date> run1` without that copy publishes the old 861
events/s as measured at the current commit against the current argus-ledger, and every gate passes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
RECORD = ("started_utc=2026-10-28T01:33:30Z\npin=taskset -c 0-11\ncommit=ee741e8\n"
          "crate_versions=harpe 0.1.0-alpha.7, kibisis 0.1.0-alpha.9, argus-ledger 0.1.0-alpha.7\n")


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bc(tmp_path, monkeypatch):
    mod = _load("build_claims")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    monkeypatch.delenv("TELARCH_RUNS_DATE", raising=False)
    one_m = tmp_path / "reports" / "runs" / "2026-10-28" / "file_ledger_1m"
    one_m.mkdir(parents=True)
    (one_m / "run_env.txt").write_text(RECORD, encoding="utf-8")
    (tmp_path / "reports" / "step3_argus_file_ledger_1m.json").write_text("{}", encoding="utf-8")   # the figure is still published
    return mod


def test_control_the_record_is_found_while_its_date_is_the_latest(bc):
    prov = bc.one_m_provenance()
    assert prov["commit"] == "ee741e8" and prov["pin"] == "taskset -c 0-11"
    assert prov["crate_versions"]["argus-ledger"] == "0.1.0-alpha.7"


def test_a_newer_runs_date_does_not_turn_the_old_run_into_a_current_one(bc, tmp_path):
    (tmp_path / "reports" / "runs" / "2026-11-12" / "run1").mkdir(parents=True)      # the next re-measure
    try:
        prov = bc.one_m_provenance()
    except (AssertionError, ValueError, SystemExit, FileNotFoundError):
        return                                   # refusing to build without the run's record is correct
    assert prov.get("commit") == "ee741e8" and prov.get("crate_versions", {}).get("argus-ledger") == "0.1.0-alpha.7", (
        f"one_m_provenance() returned {prov!r}: build() will stamp reports/step3_argus_file_ledger_1m.json with the "
        "current commit and crate versions and add no carried_over entry")
