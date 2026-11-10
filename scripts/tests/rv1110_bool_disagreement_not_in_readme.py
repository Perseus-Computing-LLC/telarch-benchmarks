"""severity: medium
finding: when runs disagree on a pass/fail claim the README row is identical to a unanimous pass

Location: scripts/build_claims.py:761-762 and :782-785 (aggregate keeps "the last run's value", sets run_count to
N and puts the disagreement in the claim's note "so it is visible rather than averaged away");
scripts/claims_check.py:98-104 (render prints statement, value, run_count, range and program, never the note).

Effect: a correctness claim that failed in 2 of 3 runs (swarm convergence, anchored-rewrite detection) is
published in README.md as "yes | 3": the reader of the published table cannot tell it from three passes, and
claims-check, drift-check and vendor-check have nothing to say about it.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
CID = "swarm_0pct_converged"


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
    return _load("build_claims")


@pytest.fixture(scope="module")
def cc():
    return _load("claims_check")


def row(cc, claims):
    return next(line for line in cc.render(claims).splitlines() if claims["claims"][CID]["statement"] in line)


def aggregated(bc, tmp_path, values):
    """The committed claims.json with CID rebuilt as build_claims.main() would from runs reporting `values`."""
    claims = copy.deepcopy(json.loads((REPO / "claims.json").read_text(encoding="utf-8")))
    c = claims["claims"][CID]
    c.update(value=values[-1], run_count=1, note="")                 # build(reports/) = the last run
    out = {"meta": claims["meta"], "claims": {CID: c}}
    dirs = []
    for i in range(len(values)):
        d = tmp_path / f"{'-'.join(map(str, values))}-run{i + 1}"
        d.mkdir()
        dirs.append(str(d))
    bc.aggregate(out, [{CID: {"value": v}} for v in values], dirs)
    return claims


def test_control_a_unanimous_claim_renders_yes_over_three_runs(bc, cc, tmp_path):
    claims = aggregated(bc, tmp_path, [True, True, True])
    assert claims["claims"][CID]["note"] == ""
    assert "| yes | 3 |" in row(cc, claims)


def test_a_claim_that_failed_in_two_of_three_runs_is_not_published_as_a_plain_pass(bc, cc, tmp_path):
    unanimous = row(cc, aggregated(bc, tmp_path, [True, True, True]))
    try:
        split = aggregated(bc, tmp_path, [False, False, True])
    except (AssertionError, ValueError, SystemExit):
        return                                                       # refusing to build is correct too
    assert "disagree" in split["claims"][CID]["note"]                 # the note exists in claims.json ...
    assert row(cc, split) != unanimous, (                             # ... and the published row hides it
        "README row for runs [False, False, True] equals the row for [True, True, True]: " + row(cc, split))
