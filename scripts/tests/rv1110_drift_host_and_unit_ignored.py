"""severity: medium
finding: drift-check compares figures across hosts and across units; the hardware line and unit it stores are never read

Location: scripts/drift_check.py:36-52 (check reads `value`, `range` and `run_count` only), :62 and :67
(make_baseline stores each figure's `unit` and the run's `hardware`), :10-12 (docstring: "Both files must come from
the same host for the comparison to mean anything; the baseline records its hardware line").

Effect: a claims.json regenerated on a faster machine passes while the code is slower (and one regenerated on a
slower machine fails for no code reason); the stored hardware line that would tell the two cases apart is not
compared. Likewise a figure whose unit changed (µs to ms) is compared number against number: 1 ms against a
2 µs baseline is reported as "improved".
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
HOST_A = "Linux x86_64, 13th Gen Intel Core i5-13500, 20 logical CPUs; Rust programs and Python scripts run under `taskset -c 0-9`"
HOST_B = "Linux x86_64, AMD EPYC 9654 96-Core Processor, 192 logical CPUs; Rust programs and Python scripts run under `taskset -c 0-9`"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def claims_file(path, hardware, value, rng, unit="µs"):
    meta = {"benchmark_commit": "abc1234", "generated": "2026-11-05T00:00:00Z", "hardware": hardware, "toolchain": "rustc",
            "crate_versions": {}, "runs": ["r1", "r2", "r3"]}
    claim = {"value": value, "range": rng, "unit": unit, "run_count": 3, "hardware": hardware}
    path.write_text(json.dumps({"meta": meta, "claims": {"harpe_small_p50_us": claim}}), encoding="utf-8")
    return path


@pytest.fixture()
def gate(tmp_path):
    dc = _load("drift_check")
    assert Path(dc.__file__).resolve().parent == SCRIPTS
    baseline = tmp_path / "baseline.json"
    made = dc.make_baseline("v-test", claims_file(tmp_path / "released.json", HOST_A, 2.0, [1.9, 2.1]), baseline)
    assert made["meta"]["hardware"] == HOST_A and made["figures"]["harpe_small_p50_us"]["unit"] == "µs"

    def run(hardware, value, rng, unit="µs"):
        cur = claims_file(tmp_path / "claims.json", hardware, value, rng, unit)
        return dc.main(["--baseline", str(baseline), "--current", str(cur)])

    return run


def test_control_same_host_same_unit(gate):
    assert gate(HOST_A, 2.05, [2.0, 2.1]) == 0
    assert gate(HOST_A, 3.0, [2.9, 3.1]) == 1


def test_figures_from_another_host_are_not_compared_as_if_from_the_same_one(gate):
    assert gate(HOST_B, 1.0, [0.9, 1.1]) != 0, (
        "baseline measured on an i5-13500, claims.json on an EPYC 9654: drift-check compares them, calls the figure "
        "'improved' and exits 0")


def test_a_figure_whose_unit_changed_is_not_compared_number_against_number(gate):
    assert gate(HOST_A, 1.0, [0.9, 1.1], unit="ms") != 0, (
        "baseline 2.0 µs, current 1.0 ms (500 times slower): drift-check reads 1.0 < 2.0, 'improved', exit 0")
