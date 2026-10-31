"""LongMemEval under windowed packing (--pair-window / --window): evidence recall and the reader's prompt.

Built from the P9 kickoff smoke (LME-S 6ae235be, single-session-assistant, pair192/mix, retrieval-only): the
answer round's 454-token assistant turn was packed as a 192-token window, yet the screen read round_recall 0.0
with session_recall 1.0, and the reader prompt was 633 tokens against the control's 8071 while Harpe had
packed 7762 tokens of history. Packed items carry `<uid>#p<k>` / `<uid>#<j>` ids, which `evidence()`
compared with round uids, and the windowed units had no turns, which is what `render_history()` renders.
"""

from pathlib import Path

import pytest

from telarch_evals.bench_longmemeval import evidence, render_history, units_for
from telarch_evals.memory import TelarchMemory

QUESTION = "Which hotel did you recommend for my Lisbon trip?"
ANSWER = "For a quiet stay I recommend the Casa Azulejo hotel in Alfama, Lisbon, a short walk from the tram."
TAIL = "Final reminder: carry a refillable water bottle on every walk."
OPENER = "Welcome back! I kept the notes from our Lisbon trip planning chat."
WINDOWED = {"pair192/mix": {"pair_window": 192, "scoring": "mix"}, "w192": {"window": 192}}


def _entry() -> dict:
    """An LME-S-shaped history: the answer is mid-way through an assistant turn far over 192 tokens."""
    packing = " ".join(f"Packing tip {i}: roll your clothes and keep chargers in one pouch." for i in range(14))
    transport = " ".join(f"Transport note {i}: buy a rechargeable card at the metro station kiosk." for i in range(18))
    answer_turn = f"{packing} {ANSWER} {transport} {TAIL}"
    return {
        "question_id": "p91_lme_windowed", "question_type": "single-session-assistant", "question": QUESTION,
        "answer": "Casa Azulejo", "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_session_ids": ["s_garden", "s_trip", "s_opener"],
        "haystack_dates": ["2023/05/01 (Mon) 09:00", "2023/05/10 (Wed) 18:30", "2023/05/20 (Sat) 08:15"],
        "answer_session_ids": ["s_trip"],
        "haystack_sessions": [
            [{"role": "user", "content": "My tomato seedlings look pale. What should I do?"},
             {"role": "assistant", "content": "Give them more light and feed them a diluted fertiliser weekly."}],
            [{"role": "user", "content": "I'm planning a trip to Lisbon next spring. Could you recommend a quiet hotel?"},
             {"role": "assistant", "content": answer_turn, "has_answer": True},
             {"role": "user", "content": "Thanks! Any tips for the hotel check-in?"},
             {"role": "assistant", "content": "Bring your passport; most hotels in Lisbon ask for it at check-in."}],
            [{"role": "assistant", "content": OPENER},
             {"role": "user", "content": "Great, can you remind me what to pack for the trip?"},
             {"role": "assistant", "content": "Light layers, comfortable shoes and a rain jacket."}],
        ],
    }


def _recall(tmp_path: Path, knobs: dict):
    entry = _entry()
    units = units_for(entry)
    mem = TelarchMemory(tmp_path, budget=8000, encrypt=False, **knobs)
    mem.open(entry["question_id"], units)
    try:
        recall = mem.recall(entry["question_id"], entry["question"], render_history)
    finally:
        mem.close(entry["question_id"])
    assert recall.units and all(u.meta.get("parent_uid") for u in recall.units), "the arm did not pack windowed items"
    return entry, units, recall


@pytest.mark.parametrize("knobs", WINDOWED.values(), ids=WINDOWED.keys())
def test_round_recall_counts_the_parent_round_of_windowed_items(tmp_path: Path, knobs: dict):
    entry, units, recall = _recall(tmp_path, knobs)
    ev = evidence(units, recall.units, entry)
    assert ev["session_recall"] == 1.0
    assert ev["round_recall"] == 1.0, "the answer round was packed, but its windowed items were not mapped to it"


@pytest.mark.parametrize("knobs", WINDOWED.values(), ids=WINDOWED.keys())
def test_reader_prompt_carries_the_packed_window(tmp_path: Path, knobs: dict):
    _, _, recall = _recall(tmp_path, knobs)
    prompt = render_history(recall.units)
    assert "Could you recommend a quiet hotel?" in prompt, "the user turn is missing from the reader's prompt"
    assert ANSWER in prompt, "the assistant window holding the answer is missing from the reader's prompt"
    assert TAIL not in prompt, "the prompt shows the whole assistant turn, not the window that was packed"
    assert "assistant: … " in prompt, "the window's cut marks are missing"
    assert recall.stats["joined_tokens_o200k"] >= recall.stats["context_tokens_o200k"] // 2


@pytest.mark.parametrize("knobs", WINDOWED.values(), ids=WINDOWED.keys())
def test_reader_prompt_keeps_each_turns_own_role(tmp_path: Path, knobs: dict):
    _, _, recall = _recall(tmp_path, knobs)
    prompt = render_history(recall.units)
    assert f"assistant: {OPENER}" in prompt, "a session's opening assistant turn is missing or relabelled"
    assert f"user: {OPENER}" not in prompt, "an assistant turn is shown to the reader as the user's"
