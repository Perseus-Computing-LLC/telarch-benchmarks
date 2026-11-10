"""severity: medium
finding: pass/fail claims are left out of every baseline, so a correctness claim that flips to "no" is never a drift

Location: scripts/drift_check.py:58-61 (make_baseline skips every bool and non-numeric claim), :40 (check walks
only the baseline's figures), so `argus_anchored_rewrite_caught` and `swarm_*_converged` are in no baselines/v*.json.

Effect: drift-check stops a PR whose regenerated claims.json has a figure well over 10 % slower, and passes one in
which "Anchored checkpoint verification catches full-chain rewrite attacks" went from yes to no. claims-check then
only requires the README to say "no" as well. A pass/fail claim turning false is invisible to the regression gate.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
BOOL_CLAIMS = ("argus_anchored_rewrite_caught", "swarm_0pct_converged")


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate(tmp_path):
    """Baseline written by drift_check's own --make-baseline code from the committed claims.json."""
    dc = _load("drift_check")
    assert Path(dc.__file__).resolve().parent == SCRIPTS
    claims = json.loads((REPO / "claims.json").read_text(encoding="utf-8"))
    released = tmp_path / "released.json"
    released.write_text(json.dumps(claims), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    dc.make_baseline("v-test", released, baseline)

    def run(mutate):
        cur = json.loads(json.dumps(claims))
        mutate(cur["claims"])
        path = tmp_path / "claims.json"
        path.write_text(json.dumps(cur), encoding="utf-8")
        return dc.main(["--baseline", str(baseline), "--current", str(path)])

    return claims, run


def test_control_the_baseline_gate_catches_a_slower_figure(gate):
    claims, run = gate
    assert all(claims["claims"][cid]["value"] is True for cid in BOOL_CLAIMS)
    assert run(lambda c: None) == 0

    def slower(c):
        c["argus_tamper_ms"].update(value=c["argus_tamper_ms"]["value"] * 2, range=None)

    assert run(slower) == 1


@pytest.mark.parametrize("cid", BOOL_CLAIMS)
def test_a_pass_fail_claim_that_turns_false_is_a_regression(gate, cid):
    claims, run = gate
    assert run(lambda c: c[cid].update(value=False)) != 0, (
        f"{cid} ({claims['claims'][cid]['statement']!r}) was true at the baseline and is false now; drift-check exits 0")
