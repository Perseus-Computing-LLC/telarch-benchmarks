"""severity: low
finding: latest_baseline() ranks a final or beta release below the alphas, so drift-check keeps an old baseline

Location: scripts/drift_check.py:30 (the sort key is the list of all integers in the file stem:
v0.1.0 -> [0, 1, 0], v0.1.0-alpha.12 -> [0, 1, 0, 12], v0.1.0-beta.1 -> [0, 1, 0, 1], v0.1.0-rc.1 -> [0, 1, 0, 1]).

Effect: the docstring promises "a re-measure for release N+1 is compared to release N without editing this
script". Once baselines/v0.1.0-beta.1.json or baselines/v0.1.0.json is committed, `make drift-check` (which
passes no --baseline) still compares against v0.1.0-alpha.12; the new baseline is never used, so a regression
against the beta or the final release is judged against the alpha's figures instead. No such file exists yet
(baselines/ holds v0.1.0-alpha.9 to alpha.12), so nothing published is affected today.
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


@pytest.fixture()
def latest(tmp_path, monkeypatch):
    dc = _load("drift_check")
    assert Path(dc.__file__).resolve().parent == SCRIPTS
    monkeypatch.setattr(dc, "ROOT", tmp_path)
    (tmp_path / "baselines").mkdir()

    def of(*names):
        for name in names:
            (tmp_path / "baselines" / f"{name}.json").write_text("{}", encoding="utf-8")
        return dc.latest_baseline().stem

    return of


def test_control_alphas_sort_numerically(latest):
    assert latest("v0.1.0-alpha.9", "v0.1.0-alpha.12", "v0.1.0-alpha.2") == "v0.1.0-alpha.12"
    assert latest("v0.1.1-alpha.1") == "v0.1.1-alpha.1"


@pytest.mark.parametrize("newer", ["v0.1.0-beta.1", "v0.1.0-rc.1", "v0.1.0"])
def test_a_later_stage_of_the_same_version_is_newer_than_its_alphas(latest, newer):
    assert latest("v0.1.0-alpha.11", "v0.1.0-alpha.12", newer) == newer
