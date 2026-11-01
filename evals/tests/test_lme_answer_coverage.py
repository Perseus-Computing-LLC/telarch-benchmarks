"""Answer-turn coverage of the reader prompt (telarch phase-P10, item P10.1).

Round recall credits a round when any part of it is packed: under --pair-window a round counts as found through any
of its pairs, and an assistant turn over 192 tokens is packed as one window of it. Neither number says how much of
an answer turn the reader received (the P9.2 blind spot). Every LongMemEval row, screen and paid, carries
evidence.answer_turns: per has_answer turn, the share of its whitespace-normalized characters that the exact reader
prompt (the string prompt_sha256 digests) holds in verbatim blocks of at least 64 characters, or whole.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from types import SimpleNamespace

from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import TelarchMemory

DATE = "2023/06/01 (Thu) 10:00"


class FakeClient:
    def __init__(self, model: str, reply: str):
        self.model, self.reply, self.calls = model, reply, []
        self.cache = None

    def describe(self) -> dict:
        return {"model": self.model}

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        return SimpleNamespace(text=self.reply, model=self.model, usage={"prompt_tokens": 3, "completion_tokens": 1},
                               cached=False)


class FakeLongMemEval:
    reader_template_cot = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
    reader_template_plain = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer:"

    def anscheck_prompt(self, task, question, answer, response, abstention=False):
        return f"gold={answer} response={response} abstention={abstention}"

    def judge_model(self, name):
        return name


def _session(*turns: tuple) -> list[dict]:
    return [{"role": role, "content": content, **({"has_answer": True} if flag and flag[0] else {})}
            for role, content, *flag in turns]


def _entry(qid: str, qtype: str, question: str, sessions: list[list[dict]], answer_sessions: list[int]) -> dict:
    ids = [f"{qid}_s{i}" for i in range(len(sessions))]
    return {"question_id": qid, "question_type": qtype, "question": question, "answer": "-", "question_date": DATE,
            "haystack_session_ids": ids,
            "haystack_dates": [f"2023/05/{i + 1:02d} (Mon) 09:00" for i in range(len(sessions))],
            "haystack_sessions": sessions, "answer_session_ids": [ids[i] for i in answer_sessions]}


def _unique_words(prefix: str, n: int) -> str:
    """Text in which no 64-character window repeats: each sentence carries its own fixed-width counter twice."""
    return " ".join(f"{prefix} {i:03d} keeps row {i:03d} apart." for i in range(n))


# Whole-round packing: one answer round shares the question's words and is packed whole; the other answer turn
# shares no word, or word form, with the question, so retrieval never finds its round.
LENS = "I bought a 100-400mm telephoto camera lens for the wildlife safari in Kenya next month."
TRIPOD = "Yesterday I also picked up a carbon tripod at that downtown shop near the river."
CONTROL_ENTRY = _entry("p10_control", "multi-session", "Which camera lens did I buy for the wildlife safari?", [
    _session(("user", LENS, True), ("assistant", "A telephoto camera lens suits a wildlife safari well.")),
    _session(("user", "My neighbour keeps hens in her yard."), ("assistant", "Fresh eggs are a treat.")),
    _session(("user", TRIPOD, True), ("assistant", "Carbon tripods are light to carry.")),
], answer_sessions=[0, 2])

# The pair-window blind spot: one round of three messages (a session can hold two assistant messages in a row), so
# --pair-window packs it as two pairs. The first (~890 tokens) carries the question's words; the second, the answer
# turn alone cut to its 192-token head (~210 tokens), has none. Together they exceed the 1000-token pack budget, so
# the second is pruned, and the round still counts as found through the first.
SEAWEED = ("Use a seaweed feed every fortnight, diluted to half strength, from early June onwards. "
           + _unique_words("Feeding step", 24))
GARDEN_USER = "About the tomato bed and which fertiliser to buy: " + _unique_words("Garden note", 85)
BLIND_ENTRY = _entry("p10_blind", "single-session-assistant", "Which fertiliser did you suggest for the tomato bed?", [
    _session(("user", GARDEN_USER), ("assistant", "For the tomato bed, a fertiliser is worth it."),
             ("assistant", SEAWEED, True)),
    _session(("user", "Can you name a good film for tonight?"), ("assistant", "Try a classic comedy.")),
], answer_sessions=[0])

# A long assistant answer turn: --pair-window packs one 192-token window of it, around the question's words.
HOTEL = "For a quiet stay I recommend the Casa Azulejo hotel in Alfama, Lisbon, a short walk from the tram."
LONG_REPLY = f"{_unique_words('Packing tip', 30)} {HOTEL} {_unique_words('Transport note', 40)}"
WINDOW_ENTRY = _entry("p10_window", "single-session-assistant", "Which hotel did you recommend for my Lisbon trip?", [
    _session(("user", "I'm planning a trip to Lisbon next spring. Could you recommend a quiet hotel?"),
             ("assistant", LONG_REPLY, True)),
    _session(("user", "My tomato seedlings look pale."), ("assistant", "Give them more light.")),
], answer_sessions=[0])


def _norm(text: str) -> str:
    return " ".join(text.split())


def _data(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "lme.json"
    path.write_text(json.dumps(list(entries)))
    return path


def _run(tmp_path: Path, data: Path, root: str, *, paid: bool = False, **knobs):
    reader = FakeClient("reader", "an answer")
    ctx = Context(runs_root=tmp_path / root, workdir=tmp_path / f"work-{root}",
                  system=TelarchMemory(tmp_path / f"work-{root}", **knobs), reader=reader,
                  judge=FakeClient("judge", "no"), meter=Meter(), workers=1, retrieval_only=not paid)
    out = bench_longmemeval.run(ctx, data_path=data, official=FakeLongMemEval())
    rows = {json.loads(line)["id"]: json.loads(line) for line in (out / "judgments.jsonl").read_text().splitlines()
            if line.strip()}
    return rows, json.loads((out / "summary.json").read_text()), reader.calls


def _turns(row: dict) -> list[dict]:
    assert "answer_turns" in row["evidence"], "the row does not say how much of each answer turn the prompt holds"
    return row["evidence"]["answer_turns"]


def test_the_answer_turn_of_a_packed_whole_round_reads_10000_and_of_an_unpacked_round_0(tmp_path):
    rows, _, _ = _run(tmp_path, _data(tmp_path, CONTROL_ENTRY), "screen")
    row = rows["p10_control"]
    assert row["evidence"]["round_recall"] == 0.5, "the fixture no longer packs exactly one of its two answer rounds"
    turns = _turns(row)
    assert [(t["session"], t["turn"], t["role"], t["chars"]) for t in turns] == [
        (0, 0, "user", len(LENS)), (2, 0, "user", len(TRIPOD))]
    assert [t["coverage_bp"] for t in turns] == [10000, 0]
    assert row["evidence"]["answer_coverage_bp"] == 5000


def test_under_pair_window_a_round_found_through_another_pair_reads_0(tmp_path):
    rows, _, prompts = _run(tmp_path, _data(tmp_path, BLIND_ENTRY), "pair", paid=True, budget=1000, pair_window=192,
                            scoring="mix")
    row = rows["p10_blind"]
    assert row["evidence"]["round_recall"] == 1.0, "the fixture no longer packs the answer round's first pair"
    (prompt,) = prompts
    assert "Garden note 084" in prompt and "seaweed" not in prompt, "the fixture no longer prunes the answer's pair"
    assert [(t["session"], t["turn"], t["role"], t["coverage_bp"]) for t in _turns(row)] == [(0, 2, "assistant", 0)]
    assert row["evidence"]["answer_coverage_bp"] == 0


def test_an_assistant_answer_turn_cut_to_a_192_token_window_reads_the_windows_share(tmp_path):
    rows, _, prompts = _run(tmp_path, _data(tmp_path, WINDOW_ENTRY), "paid", paid=True, pair_window=192,
                            scoring="mix")
    (prompt,) = prompts
    window = re.search(r"assistant: … (.+?) …\n", prompt)
    assert window and HOTEL in window.group(1), "the fixture no longer packs a window of the answer turn"
    share = 10000 * len(_norm(window.group(1))) / len(_norm(LONG_REPLY))
    (turn,) = _turns(rows["p10_window"])
    assert (turn["role"], turn["chars"]) == ("assistant", len(_norm(LONG_REPLY)))
    assert 0 < turn["coverage_bp"] < 10000
    assert abs(turn["coverage_bp"] - share) <= 100, (turn["coverage_bp"], share)


def test_paid_and_screen_rows_of_a_question_carry_the_same_coverage(tmp_path):
    data = _data(tmp_path, CONTROL_ENTRY, WINDOW_ENTRY)
    screen, _, _ = _run(tmp_path, data, "screen", pair_window=192, scoring="mix")
    paid, _, _ = _run(tmp_path, data, "paid", paid=True, pair_window=192, scoring="mix")
    for qid in ("p10_control", "p10_window"):
        assert screen[qid]["prompt_sha256"] == paid[qid]["prompt_sha256"]
        assert _turns(screen[qid]) == _turns(paid[qid])
        assert screen[qid]["evidence"]["answer_share_bp"] == paid[qid]["evidence"]["answer_share_bp"]


def test_the_summary_averages_coverage_over_non_abstention_questions(tmp_path):
    abstain = {**CONTROL_ENTRY, "question_id": "p10_control_abs",
               "haystack_session_ids": CONTROL_ENTRY["haystack_session_ids"][1:],
               "haystack_dates": CONTROL_ENTRY["haystack_dates"][1:],
               "haystack_sessions": CONTROL_ENTRY["haystack_sessions"][1:]}
    data = _data(tmp_path, CONTROL_ENTRY, WINDOW_ENTRY, abstain)
    for root, paid in (("screen", False), ("paid", True)):
        rows, summary, _ = _run(tmp_path, data, root, paid=paid, pair_window=192, scoring="mix")
        assert [t["coverage_bp"] for t in _turns(rows["p10_control_abs"])] == [0], root
        (window,) = _turns(rows["p10_window"])
        assert summary["answer_coverage_bp"] == round((5000 + window["coverage_bp"]) / 2), root
        assert (summary["answer_turns_whole"], summary["answer_turns_partial"], summary["answer_turns_absent"]) == (
            1, 1, 1), root
        assert 0 < summary["answer_share_bp"] < 10000, root


# The block matcher, through answer_coverage(entry, prompt) on hand-made prompts.

def _one_turn(text: str) -> dict:
    return _entry("m", "single-session-assistant", "q?", [_session(("assistant", text, True))], answer_sessions=[0])


def _coverage(text: str, prompt: str) -> int:
    return bench_longmemeval.answer_coverage(_one_turn(text), prompt)["answer_turns"][0]["coverage_bp"]


def test_a_block_counts_from_64_characters_and_a_short_turn_only_whole():
    turn = _unique_words("Matcher row", 8)
    assert len(turn) > 200 and "<" not in turn and ">" not in turn
    assert _coverage(turn, f"<{turn[40:103]}>") == 0  # 63 characters: not a block
    assert _coverage(turn, f"<{turn[40:104]}>") == round(10000 * 64 / len(turn))
    assert _coverage(turn, f"<{turn[40:104]}> and <{turn[150:214]}>") == round(10000 * 128 / len(turn))
    assert _coverage("Biscuit is a beagle.", "user: Biscuit is a beagle.\n") == 10000
    assert _coverage("Biscuit is a beagle.", "user: Biscuit is a\n") == 0


def test_whitespace_is_normalized_on_both_sides():
    turn = "Line one of the answer.\n\n   Line two,\twith a tab, of the same answer turn here."
    prompt = "assistant:   Line one of the answer. Line two, with a tab,\nof the same answer turn here.\n"
    assert _coverage(turn, prompt) == 10000


def test_a_long_turn_in_a_32_kb_prompt_of_frequent_characters_reads_whole_in_any_order():
    # difflib.SequenceMatcher with its default autojunk=True marks the frequent characters of a long prompt as
    # junk and splits this turn into short fragments; exact block matching must not.
    rng = random.Random(10)
    words = "the and a to of in is it that for on was with as at by an be this are from or have".split()
    filler = " ".join(rng.choice(words) for _ in range(9000))
    turn = " ".join(f"{rng.choice(words)} {i:04d}" for i in range(400))
    assert len(filler) > 30_000 and len(turn) > 3_000
    assert _coverage(turn, f"{filler[:16000]} {turn} {filler[16000:]}") == 10000
    half = len(turn) // 2
    assert _coverage(turn, f"{filler[:9000]} {turn[half:]} {filler[9000:20000]} {turn[:half]} {filler[20000:]}") == 10000


def test_coverage_reads_the_prompt_string():
    turn = _unique_words("Ledger line", 6)
    entry = _one_turn(turn)
    empty = bench_longmemeval.answer_coverage(entry, "History: nothing packed")
    assert empty["answer_turns"][0]["coverage_bp"] == 0 and empty["answer_share_bp"] == 0
    prompt = f"History:\nassistant: {turn}\nQuestion: q?"
    out = bench_longmemeval.answer_coverage(entry, prompt)
    at = round(10000 * _norm(prompt).index(turn) / len(_norm(prompt)))
    assert out["answer_turns"] == [{"session": 0, "turn": 0, "role": "assistant", "chars": len(turn),
                                    "coverage_bp": 10000, "unanchored_coverage_bp": 10000, "position_bp": at}]
    assert out["answer_coverage_bp"] == 10000
    assert (out["unanchored_coverage_bp"], out["answer_position_bp"]) == (10000, at)
    assert out["answer_share_bp"] == round(10000 * len(turn) / len(_norm(prompt)))
