"""claims.json must rebuild to the same thing on a machine other than the one that measured.

`make claims-check` rebuilds claims.json from the committed reports and run records and compares. CI runs it on
a hosted runner, which is not the measuring host: it has no /sys/devices/cpu_core/cpus (that file exists on
hybrid Intel CPUs only), and another machine may have one with a different range. The words "(performance-core
threads)" in the hardware line therefore have to come from what the runs recorded, never from the machine that
runs the check.
"""
import builtins
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
SYS_FILE = "/sys/devices/cpu_core/cpus"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _on_machine(monkeypatch, performance_cpus):
    """Make this process look like a machine whose performance-core list is `performance_cpus` (None: no file)."""
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == SYS_FILE:
            if performance_cpus is None:
                raise FileNotFoundError(SYS_FILE)
            return io.StringIO(performance_cpus + "\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


@pytest.mark.parametrize("performance_cpus", [None, "0-3", "0-63"], ids=["not-hybrid", "fewer", "more"])
def test_the_committed_claims_rebuild_identically_on_another_machine(monkeypatch, performance_cpus):
    for name in ("bench_env", "build_claims", "claims_check"):
        sys.modules.pop(name, None)
    cc = _load("claims_check")
    _on_machine(monkeypatch, performance_cpus)
    claims = json.loads((REPO / "claims.json").read_text(encoding="utf-8"))
    assert cc.report_differences(claims) == []


def test_the_hardware_line_still_says_which_threads_the_runs_were_pinned_to():
    """The fix must not be to drop the words: the runs were pinned inside the performance-core threads."""
    claims = json.loads((REPO / "claims.json").read_text(encoding="utf-8"))
    assert "`taskset -c 0-9`" in claims["meta"]["hardware"]
    assert claims["meta"]["hardware"].endswith("(performance-core threads)")


def test_what_a_run_recorded_wins_and_an_unknown_machine_gets_no_words(monkeypatch):
    sys.modules.pop("bench_env", None)
    be = _load("bench_env")
    _on_machine(monkeypatch, "0-63")                               # the building machine must not matter
    known = "Linux x86_64, 13th Gen Intel Core i5-13500, 20 logical CPUs"
    other = "Linux aarch64, Some Other CPU, 64 logical CPUs"
    assert be.performance_cpus(known) == "0-11"
    assert be.performance_cpus(known, recorded="0-7") == "0-7"     # the run's own record over the table
    assert be.performance_cpus(other) == ""
    assert be.pinning("taskset -c 0-9", be.performance_cpus(known)).endswith("(performance-core threads)")
    assert "performance-core" not in be.pinning("taskset -c 0-9", be.performance_cpus(known, recorded="0-7"))
    assert "performance-core" not in be.pinning("taskset -c 0-9", be.performance_cpus(other))
    assert "performance-core" not in be.pinning("taskset -c 0-9")
