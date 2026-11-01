"""Session-anchored answer coverage (telarch phase-P11, item P11.2).

P10.1's answer_coverage() matched an answer turn's 64-character blocks anywhere in the reader prompt, so a turn
whose opening is repeated in another packed session read as partly covered when its own session was not packed
(gpt4_59c863d7 s40 t0 in phase-P10: 1988 bp from an opening it shares with s32 t0). The blocks of answer turn (si, ti)
now match only inside the '### Session' block render_history writes for haystack session si; the P10.1 value is kept
as unanchored_coverage_bp, and each covered turn carries position_bp, where in the prompt its first covered
character sits (10000 x offset / length, on the whitespace-normalized prompt).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import TelarchMemory, Unit

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


# The anchoring case: the answer turn (session 1) opens with the same sentence as a turn of session 0. Only session 0
# shares the question's words, so only its round is packed; the opening reaches the prompt through session 0.
OPENING = "Quarterly memo for the shared house: rotate the backup drives, then label every serial."
ANCHOR_ENTRY = _entry("p11_anchor", "single-session-user", "Which bakery sells the best sourdough loaf?", [
    _session(("user", f"{OPENING} Separately, which bakery in town sells the best sourdough loaf?"),
             ("assistant", "The corner bakery bakes a fine sourdough loaf every morning.")),
    _session(("user", f"{OPENING} The spare key now hangs inside the garage cabinet, left of the door.", True),
             ("assistant", "Noted, that is a sensible spot.")),
], answer_sessions=[1])

# A packed whole answer turn.
LENS = "I bought a 100-400mm telephoto camera lens for the wildlife safari in Kenya next month."
WHOLE_ENTRY = _entry("p11_whole", "single-session-user", "Which camera lens did I buy for the wildlife safari?", [
    _session(("user", "My neighbour keeps hens in her yard."), ("assistant", "Fresh eggs are a treat.")),
    _session(("user", LENS, True), ("assistant", "A telephoto camera lens suits a wildlife safari well.")),
], answer_sessions=[1])

# A long assistant answer turn, packed as one 192-token window under --pair-window.
HOTEL = "For a quiet stay I recommend the Casa Azulejo hotel in Alfama, Lisbon, a short walk from the tram."
LONG_REPLY = f"{_unique_words('Packing tip', 30)} {HOTEL} {_unique_words('Transport note', 40)}"
WINDOW_ENTRY = _entry("p11_window", "single-session-assistant", "Which hotel did you recommend for my Lisbon trip?", [
    _session(("user", "My tomato seedlings look pale."), ("assistant", "Give them more light.")),
    _session(("user", "I'm planning a trip to Lisbon next spring. Could you recommend a quiet hotel?"),
             ("assistant", LONG_REPLY, True)),
], answer_sessions=[1])


def _position(prompt: str, text: str) -> int:
    norm = _norm(prompt)
    at = norm.find(_norm(text))
    assert at >= 0, "the text is not in the prompt"
    return round(10000 * at / len(norm))


def test_an_answer_turn_matched_only_through_another_session_reads_0_anchored(tmp_path):
    rows, summary, prompts = _run(tmp_path, _data(tmp_path, ANCHOR_ENTRY), "anchor", paid=True)
    row = rows["p11_anchor"]
    (prompt,) = prompts
    assert row["evidence"]["session_recall"] == 0.0, "the fixture packs the answer session"
    assert "bakery" in prompt and _norm(OPENING) in _norm(prompt), "the fixture no longer packs session 0"
    (turn,) = row["evidence"]["answer_turns"]
    assert (turn["session"], turn["turn"]) == (1, 0)
    assert turn["coverage_bp"] == 0, "the opening, packed in session 0, still counts for the session-1 answer turn"
    assert turn["unanchored_coverage_bp"] > 0, "the unanchored value no longer sees the shared opening"
    assert "position_bp" not in turn, "an uncovered turn carries a position"
    assert row["evidence"]["answer_coverage_bp"] == 0
    assert row["evidence"]["unanchored_coverage_bp"] == turn["unanchored_coverage_bp"]
    assert row["evidence"]["answer_share_bp"] == 0
    assert "answer_position_bp" not in row["evidence"]
    assert summary["answer_coverage_bp"] == 0
    assert summary["unanchored_coverage_bp"] == turn["unanchored_coverage_bp"]


def test_a_packed_whole_answer_turn_reads_10000_both_ways_with_its_position(tmp_path):
    rows, summary, prompts = _run(tmp_path, _data(tmp_path, WHOLE_ENTRY), "whole", paid=True)
    (prompt,) = prompts
    (turn,) = rows["p11_whole"]["evidence"]["answer_turns"]
    assert (turn["coverage_bp"], turn["unanchored_coverage_bp"]) == (10000, 10000)
    assert abs(turn["position_bp"] - _position(prompt, LENS)) <= 1
    ev = rows["p11_whole"]["evidence"]
    assert (ev["answer_coverage_bp"], ev["unanchored_coverage_bp"]) == (10000, 10000)
    assert ev["answer_position_bp"] == turn["position_bp"]
    assert (summary["answer_coverage_bp"], summary["unanchored_coverage_bp"]) == (10000, 10000)


def test_a_partly_covered_turn_carries_the_position_of_its_first_covered_character(tmp_path):
    rows, _, prompts = _run(tmp_path, _data(tmp_path, WINDOW_ENTRY), "window", paid=True, pair_window=192,
                            scoring="mix")
    (prompt,) = prompts
    window = re.search(r"assistant: … (.+?) …\n", prompt)
    assert window and HOTEL in window.group(1), "the fixture no longer packs a window of the answer turn"
    (turn,) = rows["p11_window"]["evidence"]["answer_turns"]
    assert 0 < turn["coverage_bp"] < 10000
    assert turn["coverage_bp"] == turn["unanchored_coverage_bp"]
    assert abs(turn["position_bp"] - _position(prompt, window.group(1))) <= 1


def test_screen_and_paid_rows_carry_the_same_anchored_evidence(tmp_path):
    data = _data(tmp_path, ANCHOR_ENTRY, WHOLE_ENTRY, WINDOW_ENTRY)
    screen, s_summary, _ = _run(tmp_path, data, "screen", pair_window=192, scoring="mix")
    paid, p_summary, _ = _run(tmp_path, data, "paid", paid=True, pair_window=192, scoring="mix")
    for qid in ("p11_anchor", "p11_whole", "p11_window"):
        assert screen[qid]["prompt_sha256"] == paid[qid]["prompt_sha256"]
        assert screen[qid]["evidence"] == paid[qid]["evidence"]
    assert s_summary["unanchored_coverage_bp"] == p_summary["unanchored_coverage_bp"]
    assert s_summary["unanchored_coverage_bp"] > s_summary["answer_coverage_bp"], "the anchor case moved nothing"


# The renderer: render_history's string is unchanged; the spans say which characters belong to which session.

def _render_history_p10(units: list[Unit]) -> str:
    """render_history as phase-P10 shipped it (telarch-benchmarks 325c393), kept here as the reference."""
    sessions: OrderedDict[tuple, tuple[str, list[dict]]] = OrderedDict()
    for unit in sorted(units, key=lambda u: u.order):
        sessions.setdefault(unit.order[:2], (unit.date, []))[1].extend(unit.turns)
    history = ""
    for i, (date, turns) in enumerate(sessions.values(), 1):
        sess = "".join("\n\n{}: {}".format(t["role"], t["content"].strip()) for t in turns)
        history += f"\n### Session {i}:\nSession Date: {date}\nSession Content:\n{sess}\n"
    return history


def _packed(tmp_path: Path, entry: dict, **knobs) -> list[Unit]:
    mem = TelarchMemory(tmp_path / f"pack-{entry['question_id']}-{len(knobs)}", **knobs)
    mem.open(entry["question_id"], bench_longmemeval.units_for(entry))
    try:
        return mem.recall(entry["question_id"], entry["question"], bench_longmemeval.render_history).units
    finally:
        mem.close(entry["question_id"])


def test_render_history_is_unchanged_and_the_spans_partition_it(tmp_path):
    cases = []
    for entry in (ANCHOR_ENTRY, WHOLE_ENTRY, WINDOW_ENTRY):
        everything = bench_longmemeval.units_for(entry)
        cases += [everything, everything[::-1], _packed(tmp_path, entry),
                  _packed(tmp_path, entry, pair_window=192, scoring="mix")]
    cases.append([])
    for units in cases:
        history, spans = bench_longmemeval.render_history_spans(units)
        assert history == _render_history_p10(units) == bench_longmemeval.render_history(units)
        assert sorted(spans) == sorted({u.order[1] for u in units})
        edges = sorted(spans.values())
        assert [a for a, _ in edges[:1]] == ([0] if units else [])
        assert all(b == a2 for (_, b), (a2, _) in zip(edges, edges[1:])), "the spans leave a gap or overlap"
        assert (edges[-1][1] if edges else 0) == len(history)
        for si, (a, b) in spans.items():
            block = history[a:b]
            assert block.startswith("\n### Session ") and block.endswith("\n")
            for unit in units:
                if unit.order[1] == si:
                    for t in unit.turns:
                        assert t["content"].strip() in block


def test_answer_coverage_anchors_on_the_spans_it_is_given():
    turn = _unique_words("Ledger line", 6)
    entry = _entry("m", "single-session-assistant", "q?", [
        _session(("user", "a first session")), _session(("assistant", turn, True))], answer_sessions=[1])
    prompt = f"History:\nuser: a first session\nassistant: {turn}\nQuestion: q?"
    own = prompt.index("assistant:")
    whole = bench_longmemeval.answer_coverage(entry, prompt, {0: (0, own), 1: (own, len(prompt))})
    (t,) = whole["answer_turns"]
    assert (t["coverage_bp"], t["unanchored_coverage_bp"]) == (10000, 10000)
    assert t["position_bp"] == round(10000 * _norm(prompt).index(turn) / len(_norm(prompt)))
    elsewhere = bench_longmemeval.answer_coverage(entry, prompt, {0: (own, len(prompt))})
    (t,) = elsewhere["answer_turns"]
    assert (t["coverage_bp"], t["unanchored_coverage_bp"]) == (0, 10000)
    assert elsewhere["answer_coverage_bp"] == 0 and elsewhere["answer_share_bp"] == 0
