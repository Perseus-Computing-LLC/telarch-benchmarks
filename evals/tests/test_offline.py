"""Offline tests: the memory pipeline, the three benchmark loops and the run ledger, with fake models.

Tests marked `network` load the official code from the pinned upstream files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from telarch_evals import bench_beam, bench_locomo, bench_longmemeval
from telarch_evals.common import Context, iter_json_array
from telarch_evals.llm import ChatClient, Meter, read_key, scrub
from telarch_evals.memory import FullContextMemory, TelarchMemory, Unit
from telarch_evals.official import BeamOfficial
from telarch_evals.runlog import RunLog, verify
from telarch_evals.textutil import keyword_terms, token_f1


class FakeClient:
    def __init__(self, model: str, reply):
        self.model = model
        self.reply = reply
        self.calls: list = []

    def describe(self) -> dict:
        return {"model": self.model}

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        text = self.reply(messages) if callable(self.reply) else self.reply
        return SimpleNamespace(text=text, model=self.model, usage={"prompt_tokens": 3, "completion_tokens": 1},
                               cached=False)

    def invoke(self, prompt):
        return SimpleNamespace(content=self.complete(prompt).text)


def _text(messages) -> str:
    return messages if isinstance(messages, str) else " ".join(m["content"] for m in messages)


def _ctx(tmp_path: Path, system, reader, judge) -> Context:
    return Context(runs_root=tmp_path / "runs", workdir=tmp_path / "work", system=system, reader=reader,
                   judge=judge, meter=Meter(), workers=2)


def _history(n: int = 120) -> list[Unit]:
    units = []
    for i in range(n):
        text = f"user: note {i} about gardening, tomatoes and the weather\nassistant: noted {i}"
        if i == 77:
            text = "user: I adopted a beagle named Biscuit last spring\nassistant: Congratulations on Biscuit!"
        units.append(Unit(uid=f"0:{i}", session=f"s{i // 10}", order=(i // 10, i), date=f"2023/05/{1 + i // 10:02d}",
                          text=text, rendered=f"[2023/05/{1 + i // 10:02d}]\n{text}"))
    return units


def test_keyword_terms_drops_function_words():
    assert keyword_terms("Where did I go camping last May?") == ["go", "camping", "last", "may"]
    assert keyword_terms("Who is he?") == ["who", "is", "he"]  # only stopwords: fall back to the words


def test_token_f1():
    assert token_f1("Boston", "boston.") == 1.0
    assert token_f1("in Boston, MA", "Boston") == pytest.approx(0.5)
    assert token_f1("", "x") == 0.0


def test_iter_json_array_matches_json_load(tmp_path: Path):
    data = [{"id": i, "text": "x" * (i * 37), "nested": [{"a": [1, 2, {"b": "]"}]}]} for i in range(50)]
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data, indent=1))
    assert list(iter_json_array(path, chunk_size=97)) == data


def test_read_key_from_env_file_and_scrub(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TELARCH_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nexport TELARCH_TEST_KEY='sk-test-abcdefghijklmnopqrstuvwxyz0123'\n")
    assert read_key("TELARCH_TEST_KEY", str(env)).startswith("sk-test-")
    assert "abcdefghijklmnopqrstuvwxyz" not in scrub("bad key sk-test-abcdefghijklmnopqrstuvwxyz0123")
    client = ChatClient("m", api_key_env="TELARCH_TEST_KEY", env_file=str(env))
    assert "sk-test" not in json.dumps(client.describe())


def test_telarch_memory_finds_the_needle_within_budget(tmp_path: Path):
    mem = TelarchMemory(tmp_path, budget=300)
    stats = mem.open("h", _history())
    assert stats["units"] == 120 and stats["ingest_errors"] == 0
    recall = mem.recall("h", "What is the name of the dog I adopted?")
    assert any("Biscuit" in u.text for u in recall.units)
    assert 0 < recall.stats["context_tokens"] <= 300
    assert [u.order for u in recall.units] == sorted(u.order for u in recall.units)
    mem.close("h")
    assert not list(tmp_path.glob("*.sqlite*"))


def test_plaintext_vault_ignores_operator_passphrase_and_ranks(tmp_path: Path, monkeypatch):
    # An operator passphrase in the environment must not encrypt a --plaintext vault, and a
    # plaintext vault must rank with FTS5 BM25 rather than return every hit at one score.
    monkeypatch.setenv("KIBISIS_PASSPHRASE", "operator-passphrase-0123456789abcdef")
    mem = TelarchMemory(tmp_path, encrypt=False)
    assert "KIBISIS_PASSPHRASE" not in os.environ
    mem.open("h", _history())
    vault = mem._open["h"][0]
    assert vault.crypto_provider is None
    ranks = {round(r.rank_score, 6) for r in vault.search("biscuit", scope="eval", limit=5, include_unverified=True)}
    ranks |= {round(r.rank_score, 6) for r in vault.search("gardening", scope="eval", limit=5, include_unverified=True)}
    assert len(ranks) > 1
    assert mem.config()["kibisis_search"].startswith("SQLite FTS5")
    mem.close("h")


def test_tied_top_candidates_stay_within_budget(tmp_path: Path):
    # Equally relevant units larger than the budget together: none may be pinned past it.
    units = [Unit(uid=f"0:{i}", session="s", order=(0, i), date="d",
                  text=f"user: the quarterly launch plan {i} " + "detail " * 400,
                  rendered=f"[d]\nuser: the quarterly launch plan {i} " + "detail " * 400) for i in range(6)]
    mem = TelarchMemory(tmp_path, budget=1000)
    mem.open("h", units)
    recall = mem.recall("h", "What is the quarterly launch plan?")
    assert recall.stats["context_tokens"] <= 1000
    mem.close("h")


def test_full_context_keeps_the_most_recent_units():
    mem = FullContextMemory(budget=60)
    units = _history(20)
    mem.open("h", units)
    recall = mem.recall("h", "anything")
    assert recall.units and recall.units[-1].uid == units[-1].uid
    assert recall.stats["truncated"] and recall.stats["context_tokens"] <= 60


class FakeLongMemEval:
    reader_template_cot = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
    reader_template_plain = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer:"

    def anscheck_prompt(self, task, question, answer, response, abstention=False):
        return f"gold={answer} response={response} abstention={abstention}"

    def judge_model(self, name):
        return name


def _lme_data(tmp_path: Path) -> Path:
    session_a = [{"role": "user", "content": "I adopted a beagle named Biscuit.", "has_answer": True},
                 {"role": "assistant", "content": "Congratulations!"}]
    session_b = [{"role": "user", "content": "Any tips for growing tomatoes?"},
                 {"role": "assistant", "content": "Plenty of sun."}]
    base = {"question_date": "2023/06/01 (Thu) 10:00", "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2023/05/01 (Mon) 09:00", "2023/05/02 (Tue) 09:00"],
            "haystack_sessions": [session_a, session_b]}
    data = [
        {**base, "question_id": "q1", "question_type": "single-session-user", "question": "What breed is Biscuit?",
         "answer": "beagle", "answer_session_ids": ["s1"]},
        {**base, "question_id": "q2_abs", "question_type": "single-session-user",
         "question": "What breed is my cat Mittens?", "answer": "You did not mention a cat.", "answer_session_ids": []},
    ]
    path = tmp_path / "lme.json"
    path.write_text(json.dumps(data))
    return path


def test_longmemeval_run_resume_and_ledger(tmp_path: Path):
    reader = FakeClient("reader", lambda m: "It is a beagle." if "beagle" in _text(m) and "Biscuit?" in _text(m)
                        else "You never mentioned a cat.")
    judge = FakeClient("gpt-4o-2024-08-06", lambda m: "yes" if "response=It is a beagle." in _text(m)
                       or "abstention=True" in _text(m) else "no")
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), reader, judge)
    out = bench_longmemeval.run(ctx, data_path=_lme_data(tmp_path), official=FakeLongMemEval())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["complete"] and summary["rows"] == 2
    assert summary["overall_accuracy"] == 1.0 and summary["abstention_accuracy"]["n"] == 1
    assert summary["evidence_session_recall"] == 1.0
    prediction = json.loads((out / "predictions.jsonl").read_text().splitlines()[0])
    assert set(prediction) == {"question_id", "hypothesis"}
    assert "### Session 1:\nSession Date: 2023/05/01 (Mon) 09:00" in _text(reader.calls[0])
    assert os.stat(out / "judgments.jsonl").st_mode & 0o077 == 0

    # Resuming a finished run calls no model again.
    calls = len(reader.calls)
    ctx.resume = out
    bench_longmemeval.run(ctx, data_path=_lme_data(tmp_path), official=FakeLongMemEval())
    assert len(reader.calls) == calls

    report = verify(tmp_path / "runs")
    assert report["valid"] and report["chain_valid"] and len(report["runs"]) == 2

    with open(out / "judgments.jsonl", "a") as fh:
        fh.write('{"id": "forged"}\n')
    report = verify(tmp_path / "runs")
    assert not report["valid"] and "judgments.jsonl" in report["runs"][-1]["mismatched"]


def test_verify_accepts_a_run_completed_again_after_resume(tmp_path: Path):
    # A run stops with failed rows (a billing error, or its spend cap) and completes; a resume drops
    # the failed rows, rewriting the run's files, and the run completes again.
    runs = tmp_path / "runs"
    config = {"system": "telarch", "benchmark": "longmemeval"}
    first = RunLog(runs, "longmemeval", config, sources=[])
    first.write({"id": "q1", "status": "ok"})
    first.write({"id": "q2", "status": "error", "error": "billing"})
    first.finish({"rows": 1, "errors": 1, "complete": False})
    assert verify(runs)["valid"]

    resumed = RunLog(runs, "longmemeval", config, sources=[], resume=first.dir)
    assert resumed.done == {"q1"}
    report = verify(runs)  # resumed but not completed again: still checked against the first completion
    assert not report["valid"] and report["runs"][-1]["mismatched"] == ["judgments.jsonl"]

    resumed.write({"id": "q2", "status": "ok"})
    resumed.finish({"rows": 2, "errors": 0, "complete": True})
    report = verify(runs)
    assert report["valid"] and report["chain_valid"]
    superseded, latest = report["runs"]
    assert superseded["superseded_by"] == latest["sequence"] and "files_match" not in superseded
    assert latest["files_match"] and latest["mismatched"] == []

    (first.dir / "summary.json").write_text("{}\n")  # an edit after the latest completion is still caught
    report = verify(runs)
    assert not report["valid"] and report["runs"][-1]["mismatched"] == ["summary.json"]


def test_longmemeval_errors_are_recorded_not_scored(tmp_path: Path):
    def broken(_):
        raise RuntimeError("provider down")

    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), FakeClient("reader", broken), FakeClient("judge", "yes"))
    out = bench_longmemeval.run(ctx, data_path=_lme_data(tmp_path), official=FakeLongMemEval())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["errors"] == 2 and summary["rows"] == 0 and not summary["complete"]


class FakeJudgePrompt:
    def render(self, question, gold, answer):
        return f"{gold}|{answer}"

    label = staticmethod(lambda text: json.loads(text)["label"])


def test_locomo_run(tmp_path: Path):
    sample = {"sample_id": "conv-1", "conversation": {
        "speaker_a": "Ann", "speaker_b": "Bo", "session_1_date_time": "1:00 pm on 8 May, 2023",
        "session_1": [{"speaker": "Ann", "dia_id": "D1:1", "text": "I ran a marathon in Boston last week."},
                      {"speaker": "Bo", "dia_id": "D1:2", "text": "Amazing!", "blip_caption": "a medal"}]},
        "qa": [{"question": "Which city did Ann run a marathon in?", "answer": "Boston", "evidence": ["D1:1"],
                "category": 4},
               {"question": "What did Bo run?", "adversarial_answer": "Not mentioned", "category": 5}]}
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]))
    reader = FakeClient("reader", "Boston")
    judge = FakeClient("gpt-4o-mini", lambda m: '{"label": "CORRECT"}' if "Boston|Boston" in _text(m) else '{"label": "WRONG"}')
    out = bench_locomo.run(_ctx(tmp_path, TelarchMemory(tmp_path / "work"), reader, judge), data_path=path,
                           judge_prompt=FakeJudgePrompt())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rows"] == 1 and summary["j_overall"] == 1.0 and summary["f1_overall"] == 1.0
    assert summary["evidence_recall"] == 1.0 and summary["by_category"]["single-hop"]["n"] == 1
    assert "[1:00 pm on 8 May, 2023] Ann: I ran a marathon in Boston" in _text(reader.calls[0])


class FakeBeam:
    score = staticmethod(BeamOfficial.score)

    def answer(self, context, question):
        return f"{context}\n\n{question}"

    def evaluate(self, ability, rubric, response, question, judge):
        judge.invoke(f"{rubric} {response}")
        if ability == "event_ordering":
            return {"tau_norm": 0.5, "llm_judge_score": 1.0}
        return {"llm_judge_score": 1.0 if "Lisbon" in response else 0.0}


def test_beam_run_and_10m_layout(tmp_path: Path):
    turns = [[{"role": "user", "content": "I am moving to Lisbon in July.", "time_anchor": "March-15-2024"},
              {"role": "assistant", "content": "Lisbon is lovely in summer."}],
             [{"role": "user", "content": "First I will sell my car.", "time_anchor": "March-16-2024"},
              {"role": "assistant", "content": "Good plan."}]]
    conv = tmp_path / "beam" / "1"
    (conv / "probing_questions").mkdir(parents=True)
    (conv / "chat.json").write_text(json.dumps([{"batch_number": 1, "turns": turns}]))
    (conv / "probing_questions" / "probing_questions.json").write_text(json.dumps({
        "information_extraction": [{"question": "Where am I moving?", "rubric": ["Lisbon"]}],
        "event_ordering": [{"question": "Order my plans: moving and selling", "rubric": ["sell car", "move"]}]}))
    reader = FakeClient("reader", lambda m: "Lisbon" if "Lisbon" in _text(m) else "unknown")
    out = bench_beam.run(_ctx(tmp_path, TelarchMemory(tmp_path / "work"), reader, FakeClient("gpt-4.1-mini", "{}")),
                         data_dir=tmp_path / "beam", official=FakeBeam(), abilities=["information_extraction", "event_ordering"])
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rows"] == 2 and summary["by_ability"]["information_extraction"]["score"] == 1.0
    assert summary["by_ability"]["event_ordering"]["score"] == 0.5 and summary["overall"] == 0.75

    nested = [{"plan-1": [{"batch_number": 1, "turns": turns}]}, {"plan-2": [{"batch_number": 1, "turns": turns[:1]}]}]
    units = bench_beam.units_for(nested)
    assert len(units) == 3 and units[0].date == "March-15-2024" and units[2].session.startswith("plan-2")


@pytest.mark.network
def test_official_code_loads_from_pinned_sources():
    from telarch_evals.official import LoCoMoJudge, LongMemEvalOfficial

    lme = LongMemEvalOfficial()
    assert lme.judge_model("gpt-4o") == "gpt-4o-2024-08-06"
    assert lme.reader_template_cot.count("{}") == 3 and "step by step" in lme.reader_template_cot
    assert "Answer yes or no only" in lme.anscheck_prompt("multi-session", "q", "a", "r")
    assert LoCoMoJudge.label('{"label": "CORRECT"}') == "CORRECT"
    beam = BeamOfficial()
    assert len(beam.evaluators) == 10
