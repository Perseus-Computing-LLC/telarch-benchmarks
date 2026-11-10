"""severity: low
finding: without the jsonschema package, which neither CI nor the Makefile installs, claims.json is checked for key presence only

Location: scripts/build_claims.py:71-86 (the fallback compares the `required` lists and nothing else);
.github/workflows/ci.yml:38 (the benchmark job installs pydantic, cryptography, sqlcipher3-binary, pytest and
hatchling; jsonschema appears nowhere in the workflow, the Makefile or CONTRIBUTING.md). build_claims.py:14 says
the schema is applied "when the jsonschema package is installed", so the fallback is declared; what is not
declared is that it is the only path any documented or automated invocation takes. No other script validates a
committed claims.json against claims_schema.json at all.

Effect: claims_schema.json promises a numeric/boolean/string `value`, an integer `run_count` and a two-number
`range`. On the paths that run, a claim with `value: null`, `run_count: "3"` or a three-number range is written
and published; `value: null` then also passes drift-check (see rv1110_drift_missing_figure_passes.py).

The test puts check() in the environment CI gives it by making `import jsonschema` fail.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
CLAIM = "harpe_small_p50_us"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bc(monkeypatch):
    mod = _load("build_claims")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    monkeypatch.setitem(sys.modules, "jsonschema", None)          # `import jsonschema` raises ImportError, as in CI
    return mod


@pytest.fixture()
def claims():
    return json.loads((REPO / "claims.json").read_text(encoding="utf-8"))


def test_control_the_committed_claims_pass_and_a_missing_key_is_refused(bc, claims):
    bc.check(copy.deepcopy(claims))
    del claims["claims"][CLAIM]["unit"]
    with pytest.raises(AssertionError, match="lacks unit"):
        bc.check(claims)


@pytest.mark.parametrize("field, bad", [("value", None), ("run_count", "3"), ("range", [1.0, 2.0, 3.0]), ("value", [2.0])],
                         ids=["value null", "run_count is text", "range of three", "value is a list"])
def test_a_claim_the_schema_forbids_is_refused(bc, claims, field, bad):
    claims["claims"][CLAIM][field] = bad
    with pytest.raises(Exception):
        bc.check(claims)
