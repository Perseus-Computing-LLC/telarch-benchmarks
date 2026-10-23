#!/usr/bin/env python3
"""Gap 4: Python state-machine model check of the Kibisis lease protocol.

A breadth-first search over a small Python model of the lease and fencing-token protocol, written to
mirror spec/kibisis_lease_fencing.tla; it does not run the TLA+ tools. Bounds: 3 workers, 4 clock
ticks, 4 fencing tokens. In every reachable state it checks:
   - MutualExclusion: while a lease is live, no other worker holds the current fencing token.
   - MonotonicFencing: the sink never holds a token above the last one issued.
The model's sink keeps the highest token it has seen, so a stale write cannot replace a newer one by
construction; gap3 tests the late-write rule across OS processes.
"""

import collections
import json
import os
import sys
import time
from typing import NamedTuple, Set, List, Dict, Any

import bench_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class State(NamedTuple):
    lease_holder: str
    lease_expires_at: int
    current_fencing_token: int
    storage_highest_token: int
    w1_held_token: int
    w2_held_token: int
    w3_held_token: int
    clock: int


def model_check(max_time: int = 4, max_token: int = 4) -> Dict[str, Any]:
    init_state = State(
        lease_holder="None",
        lease_expires_at=0,
        current_fencing_token=0,
        storage_highest_token=0,
        w1_held_token=0,
        w2_held_token=0,
        w3_held_token=0,
        clock=0,
    )

    visited: Set[State] = {init_state}
    queue = collections.deque([init_state])
    transitions_checked = 0

    workers = ["W1", "W2", "W3"]

    def get_token(s: State, w: str) -> int:
        if w == "W1":
            return s.w1_held_token
        elif w == "W2":
            return s.w2_held_token
        else:
            return s.w3_held_token

    def set_token(s: State, w: str, t: int) -> State:
        if w == "W1":
            return s._replace(w1_held_token=t)
        elif w == "W2":
            return s._replace(w2_held_token=t)
        else:
            return s._replace(w3_held_token=t)

    # Invariant checkers
    def check_invariants(s: State) -> None:
        # Invariant 1: Mutual Exclusion
        if s.lease_holder != "None" and s.lease_expires_at >= s.clock:
            for w in workers:
                if w != s.lease_holder:
                    assert get_token(s, w) < s.current_fencing_token, f"Mutual exclusion violated: {s}"

        # Invariant 2: Monotonic Fencing
        assert s.storage_highest_token <= s.current_fencing_token, f"Monotonic fencing violated: {s}"

    while queue:
        s = queue.popleft()
        check_invariants(s)

        next_states: List[State] = []

        # 1. Action: Acquire(w, ttl)
        for w in workers:
            can_acquire = (
                s.lease_holder == "None"
                or s.lease_expires_at < s.clock
                or s.lease_holder == w
            )
            if can_acquire and s.current_fencing_token < max_token:
                for ttl in [1, 2]:
                    if s.clock + ttl <= max_time:
                        new_fencing = s.current_fencing_token + 1
                        s_next = s._replace(
                            lease_holder=w,
                            lease_expires_at=s.clock + ttl,
                            current_fencing_token=new_fencing,
                        )
                        s_next = set_token(s_next, w, new_fencing)
                        next_states.append(s_next)

        # 2. Action: Release(w)
        if s.lease_holder != "None" and s.lease_expires_at >= s.clock:
            s_next = s._replace(lease_holder="None", lease_expires_at=0)
            next_states.append(s_next)

        # 3. Action: WriteToSink(w)
        for w in workers:
            token = get_token(s, w)
            if token > 0:
                # Fencing-aware sink accepts if token > storage_highest_token
                new_storage = max(s.storage_highest_token, token)
                s_next = s._replace(storage_highest_token=new_storage)
                next_states.append(s_next)

        # 4. Action: Tick
        if s.clock < max_time:
            s_next = s._replace(clock=s.clock + 1)
            next_states.append(s_next)

        # Enqueue unvisited states
        for ns in next_states:
            transitions_checked += 1
            if ns not in visited:
                visited.add(ns)
                queue.append(ns)

    return {
        "reachable_states_explored": len(visited),
        "state_transitions_checked": transitions_checked,
        "max_time_steps": max_time,
        "max_fencing_tokens": max_token,
        "worker_count": len(workers),
        "invariants_verified": [
            "MutualExclusion",
            "MonotonicFencing",
        ],
        "all_invariants_hold": True,
    }


def main():
    print("=" * 80)
    print("     TELARCH GAP 4: KIBISIS LEASE STATE-MACHINE MODEL CHECK     ")
    print("  Python state-machine model mirroring spec/kibisis_lease_fencing.tla (not TLC)  ")
    print("================================================================\n")

    t0 = time.perf_counter()
    res = model_check(max_time=4, max_token=4)
    elapsed = time.perf_counter() - t0

    print("Model check results (bounds: 4 clock ticks, 4 fencing tokens, 3 workers):")
    print(f"  • Distinct Reachable States:          {res['reachable_states_explored']:,}")
    print(f"  • State Transitions Checked:          {res['state_transitions_checked']:,}")
    print(f"  • Check Time:                         {elapsed * 1000.0:.2f} ms")
    print(f"  • Invariants Checked:                 {', '.join(res['invariants_verified'])}")
    print("  • Result:                             no violation in any reachable state within these bounds")

    report = {
        "benchmark": "telarch-gap4-kibisis-model-check",
        "suite": "Telarch Suite (TCI)",
        "host": bench_env.hardware(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "formal_spec_path": "spec/kibisis_lease_fencing.tla",
        "model_check_summary": res,
        "verification_duration_ms": round(elapsed * 1000.0, 2),
        "miri_status": "configured for CI nightly runner",
    }

    out_file = os.path.join(ROOT, "reports", "gap4_kibisis_model_check.json")
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[REPORT] Saved machine-readable report to: {out_file}")


if __name__ == "__main__":
    main()
