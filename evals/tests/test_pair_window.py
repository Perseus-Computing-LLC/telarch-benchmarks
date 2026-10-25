"""Tests for role-asymmetric turn packing (--pair-window)."""

from pathlib import Path
import pytest
from telarch_evals.memory import TelarchMemory, Unit


def test_pair_window_preserves_user_turns_whole(tmp_path: Path):
    # Long user message, long assistant message
    u_long = "Please remember this critical detail: " + "alpha " * 150
    a_long = "I understand and will remember that: " + "beta " * 300
    units = [
        Unit(
            uid="conv:0", session="s", order=(0,), date="2026-01-01",
            text=f"user: {u_long}\nassistant: {a_long}",
            rendered=f"[2026-01-01]\nuser: {u_long}\nassistant: {a_long}",
            turns=[{"role": "user", "content": u_long}, {"role": "assistant", "content": a_long}]
        )
    ]
    mem = TelarchMemory(tmp_path, budget=8000, pair_window=192, scoring="mix", encrypt=False)
    ingest = mem.open("test_hist", units)
    assert ingest["units"] == 1
    recall = mem.recall("test_hist", "alpha detail")
    assert len(recall.units) == 1
    packed = recall.units[0].rendered
    # Invariant 1: User message must be preserved completely whole
    assert u_long.strip() in packed, "User turn was truncated!"
    # Invariant 2: Assistant message should be windowed
    assert "…" in packed, "Assistant turn was not windowed!"
    # Invariant 3: Rendered contains user and assistant
    assert packed.startswith("[2026-01-01]\nuser: ")
    assert "\nassistant: " in packed
    mem.close("test_hist")


def test_pair_window_scoring_variants(tmp_path: Path):
    units = [
        Unit(
            uid=f"conv:{i}", session="s", order=(i,), date="",
            text=f"user: query keyword {i}\nassistant: response {i}",
            rendered=f"user: query keyword {i}\nassistant: response {i}",
            turns=[{"role": "user", "content": f"query keyword {i}"}, {"role": "assistant", "content": f"response {i}"}]
        )
        for i in range(5)
    ]
    for scoring in ("kib", "m25", "mix"):
        mem = TelarchMemory(tmp_path / scoring, budget=8000, pair_window=192, scoring=scoring, encrypt=False)
        mem.open("h", units)
        recall = mem.recall("h", "query keyword 2")
        assert len(recall.units) > 0
        mem.close("h")


def test_pair_window_knapsack_guard_bounds_joined_tokens(tmp_path: Path):
    # Verify that pack_budget accounts for delimiter overhead
    units = [
        Unit(
            uid=f"conv:{i}", session="s", order=(i,), date="",
            text=f"user: statement {i}\nassistant: confirmation {i}",
            rendered=f"user: statement {i}\nassistant: confirmation {i}",
            turns=[{"role": "user", "content": f"statement {i}"}, {"role": "assistant", "content": f"confirmation {i}"}]
        )
        for i in range(10)
    ]
    mem = TelarchMemory(tmp_path, budget=1000, pair_window=192, scoring="mix", encrypt=False)
    mem.open("h", units)
    recall = mem.recall("h", "statement")
    joined = "\n\n".join(u.rendered for u in recall.units)
    toks = len(mem._enc.encode(joined, disallowed_special=()))
    assert toks <= 1000
    mem.close("h")
