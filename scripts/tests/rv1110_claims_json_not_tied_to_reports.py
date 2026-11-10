"""severity: high
finding: no gate ties claims.json to the reports it names; an edited value passes claims-check, drift-check and vendor-check

Location: scripts/claims_check.py:138-190 (compares README.md with claims.json only), scripts/drift_check.py:36-52
(compares claims.json with baselines/ only, and an "improved" value is never flagged, scripts/bench_figures.py:67-68,74),
scripts/vendor_check.py:49-80 (versions only), .github/workflows/ci.yml:53-63 and :73-77 (CI runs those three
gates, then `make bench` regenerates claims.json on the runner and compares it with nothing), Makefile:17 (`all`
has no such step either). scripts/build_claims.py is the only code that reads reports/, and nothing runs it
against the committed reports to compare the result with the committed claims.json.

Effect: build_claims.py's docstring says "claims.json always matches the reports committed beside it", but
that holds only at the moment it is run. A claims.json whose harpe_small_p50_us is typed in as 1.5 µs (the three
committed runs give a median of 2.026 µs), with the README block re-rendered from it, is accepted by every gate
CI runs. Each claim's raw_data_path and meta.runs point at reports that say something else.
"""
import importlib.util
import json
import shutil
import statistics
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
CID = "harpe_small_p50_us"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """A copy of what the three gates read, with each gate's ROOT pointed at it."""
    root = tmp_path / "repo"
    (root / "evals").mkdir(parents=True)
    for rel in ("claims.json", "README.md", "evals/README.md"):
        shutil.copy2(REPO / rel, root / rel)
    shutil.copytree(REPO / "baselines", root / "baselines")
    for crate in ("harpe", "kibisis", "argus-ledger"):
        (root / crate).mkdir()
        shutil.copy2(REPO / crate / "Cargo.toml", root / crate / "Cargo.toml")
    claims = json.loads((root / "claims.json").read_text(encoding="utf-8"))
    for c in claims["claims"].values():                      # the run record a carried-over claim points at
        run = (c.get("carried_over") or {}).get("run")
        if run:
            (root / run).mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / run / "run_env.txt", root / run / "run_env.txt")
    cc, dc, vc = _load("claims_check"), _load("drift_check"), _load("vendor_check")
    monkeypatch.setattr(cc, "ROOT", str(root))
    monkeypatch.setattr(cc, "README", str(root / "README.md"))
    monkeypatch.setattr(cc, "PROSE", [str(root / "README.md"), str(root / "evals" / "README.md")])
    monkeypatch.setattr(dc, "ROOT", root)
    monkeypatch.setattr(vc, "ROOT", str(root))

    def gates(*claims_check_args):
        monkeypatch.setattr(sys, "argv", ["claims_check.py", *claims_check_args])
        if claims_check_args:
            return {"claims-check --write": cc.main()}
        return {"claims-check": cc.main(), "drift-check": dc.main(["--current", str(root / "claims.json")]),
                "vendor-check": vc.main()}

    return root, gates


def test_control_the_committed_files_pass_and_the_reports_give_the_committed_value(tree):
    root, gates = tree
    assert gates() == {"claims-check": 0, "drift-check": 0, "vendor-check": 0}
    bc = _load("build_claims")
    assert Path(bc.ROOT) == REPO
    runs = bc.discover_runs()
    per_run = [bc.build(d)["claims"][CID]["value"] for d in runs]
    committed = json.loads((REPO / "claims.json").read_text(encoding="utf-8"))["claims"][CID]
    assert len(runs) == committed["run_count"] == 3
    assert statistics.median(per_run) == committed["value"] == 2.026     # what the named reports say


def test_a_value_the_reports_do_not_contain_is_rejected_by_some_gate(tree):
    root, gates = tree
    path = root / "claims.json"
    claims = json.loads(path.read_text(encoding="utf-8"))
    claims["claims"][CID].update(value=1.5, median=1.5, range=[1.45, 1.55])   # no run measured anything below 1.943
    path.write_text(json.dumps(claims, indent=2) + "\n", encoding="utf-8")
    assert gates("--write") == {"claims-check --write": 0}                     # README block follows claims.json
    assert "| 1.5 µs | 3 | 1.45 to 1.55 |" in (root / "README.md").read_text(encoding="utf-8")
    results = gates()
    assert any(rc != 0 for rc in results.values()), (
        f"{CID} published as 1.5 µs while reports/runs/2026-11-05/run1..3 give a median of 2.026 µs; gates: {results}")
