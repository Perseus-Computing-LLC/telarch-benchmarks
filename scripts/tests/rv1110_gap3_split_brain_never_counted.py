"""severity: high
finding: gap3 reports split_brain_violations as a constant 0, so the published kib_multiproc_split_brain claim cannot fail

Location: scripts/gap3_kibisis_multiproc_crashes.py:139 (`split_brain_count = 0`), :151-156 (the overlap check
is a `pass`), :163 (the untouched counter is written to the report); scripts/build_claims.py:519-522 sums that
field into the claim `kib_multiproc_split_brain` ("Split-brain violations across 2 to 32 concurrent OS child
processes"), which README.md prints as 0 over 3 runs.

Effect: the number is not a measurement. A coordinator that hands the lease to every process at the same time
still yields 0 violations, and the same holds when every acquire raises (l.88-90 counts any exception as a
conflict). The tests below swap in such a coordinator and ask the analysis for the violations it claims to count.
"""
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
WORKERS, ITERATIONS = 2, 20


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class GrantsEveryone:
    """A broken coordinator: every acquire succeeds at once with a fresh, larger token and a lease that stays
    valid for a second. Two processes therefore hold `shared_cluster_lock` at the same time on every iteration."""

    def __init__(self, path):
        self.path = path

    def acquire(self, resource, holder, ttl_seconds=1.0):
        return SimpleNamespace(fencing_token=time.time_ns(), holder_id=holder, expires_at=time.time() + ttl_seconds)

    def release(self, lease):
        pass


class SameTokenForEveryone(GrantsEveryone):
    def acquire(self, resource, holder, ttl_seconds=1.0):
        return SimpleNamespace(fencing_token=7, holder_id=holder, expires_at=time.time() + ttl_seconds)


@pytest.fixture()
def gap3(monkeypatch):
    mod = _load("gap3_kibisis_multiproc_crashes")
    assert Path(mod.__file__).resolve().parent == SCRIPTS
    return mod


def test_control_the_fake_coordinator_reaches_the_analysis(gap3, monkeypatch):
    """Worker processes use the swapped coordinator and their history reaches the invariant analysis: one token
    handed to everyone is counted as a rollback on every event after the first."""
    monkeypatch.setattr(gap3, "FileLeaseCoordinator", SameTokenForEveryone)
    out = gap3.run_multiproc_contention(WORKERS, ITERATIONS)
    assert out["successful_leases"] == WORKERS * ITERATIONS
    assert out["fencing_token_rollbacks"] == WORKERS * ITERATIONS - 1


def test_two_processes_holding_the_lease_at_once_is_a_split_brain(gap3, monkeypatch):
    monkeypatch.setattr(gap3, "FileLeaseCoordinator", GrantsEveryone)
    out = gap3.run_multiproc_contention(WORKERS, ITERATIONS)
    assert out["successful_leases"] == WORKERS * ITERATIONS and out["conflicts_blocked"] == 0
    # 40 leases of one resource, each valid for 1 s, all granted within a fraction of a second to two holders.
    assert out["split_brain_violations"] > 0, (
        "every one of the 40 one-second leases overlapped another holder's, and the report says "
        f"split_brain_violations={out['split_brain_violations']}")
