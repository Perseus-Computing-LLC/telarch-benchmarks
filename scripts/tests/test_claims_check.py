"""claims_check.retired_names(): README.md and evals/README.md must not use retired product names.

The published figures live in those two files, so `make claims-check` (and CI) fail when either names
a product that is no longer used publicly. "Perseus Computing" is the company and stays, as does the
Codex marketplace name `perseus-telarch-plugins`.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cc():
    spec = importlib.util.spec_from_file_location("claims_check", SCRIPTS / "claims_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claims_check"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("text", [
    "| Perseus Vault (Historical Baseline) | 0.584 |",
    "| All-30 Macro AA | Delta vs Perseus |",
    "Part of the Perseus suite",
    "Perseus Ledger",
    "a mimir store",
    "Plutus",
    "Mneme",
])
def test_retired_names_are_flagged(cc, text):
    assert cc.retired_names(text)


@pytest.mark.parametrize("text", [
    "The figures Perseus Computing quotes in collateral",
    "https://github.com/Perseus-Computing-LLC/telarch-benchmarks",
    "codex plugin add telarch-local@perseus-telarch-plugins",
    "| Mnemosyne (v3.14.0) | 0.345 |",
])
def test_company_and_marketplace_names_pass(cc, text):
    assert cc.retired_names(text) == []


def test_reports_line_numbers(cc):
    assert cc.retired_names("ok\nok\n| Perseus Vault | 0.550 |\n") == [(3, "Perseus")]


def test_published_prose_is_clean(cc):
    assert [Path(p).name for p in cc.PROSE] == ["README.md", "README.md"]
    for path in cc.PROSE:
        with open(path, encoding="utf-8") as fh:
            assert cc.retired_names(fh.read()) == [], path
