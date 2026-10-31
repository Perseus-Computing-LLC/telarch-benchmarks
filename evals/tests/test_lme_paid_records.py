"""A paid LongMemEval run records what a reader-in-the-loop control band reads (telarch phase-P9, item P9.3).

The band's bars read two things from the run files. Every manifest must say the run bypassed the response cache
(``--no-cache``), so the five control runs are five fresh reader and judge draws, not one draw replayed. And each
question's reader prompt must be identifiable, so the runs can be shown to differ only in the reader and judge: a paid
row carries ``prompt_sha256``, computed the way a retrieval-only screen row computes it, so a paid run can also be
matched question by question against a $0 screen of the same harness. Before this change a paid row had no prompt
digest and the manifest stored no cache setting.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import TelarchMemory


class FakeClient:
    def __init__(self, model: str, reply):
        self.model, self.reply, self.calls = model, reply, []
        self.cache = None

    def describe(self) -> dict:
        return {"model": self.model}

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        text = self.reply(messages) if callable(self.reply) else self.reply
        return SimpleNamespace(text=text, model=self.model, usage={"prompt_tokens": 3, "completion_tokens": 1}, cached=False)


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
        {**base, "question_id": "q2", "question_type": "single-session-user", "question": "What do tomatoes need?",
         "answer": "sun", "answer_session_ids": ["s2"]},
    ]
    path = tmp_path / "lme.json"
    path.write_text(json.dumps(data))
    return path


def _ctx(tmp_path: Path, root: str, reader, judge, **kw) -> Context:
    return Context(runs_root=tmp_path / root, workdir=tmp_path / "work", system=TelarchMemory(tmp_path / "work"),
                   reader=reader, judge=judge, meter=Meter(), workers=2, **kw)


def _rows(run_dir: Path) -> dict[str, dict]:
    return {json.loads(line)["id"]: json.loads(line) for line in (run_dir / "judgments.jsonl").read_text().splitlines()
            if line.strip()}


def test_paid_rows_carry_the_digest_of_the_prompt_the_reader_received(tmp_path):
    reader = FakeClient("reader", "an answer")
    out = bench_longmemeval.run(_ctx(tmp_path, "runs", reader, FakeClient("judge", "yes")),
                                data_path=_lme_data(tmp_path), official=FakeLongMemEval())
    rows = _rows(out)
    assert len(rows) == 2 and all(r["status"] == "ok" for r in rows.values())
    sent = {hashlib.sha256(p.encode("utf-8")).hexdigest() for p in reader.calls}
    assert {r["prompt_sha256"] for r in rows.values()} == sent and len(sent) == 2


def test_a_paid_row_and_a_screen_row_of_one_question_agree_on_the_digest(tmp_path):
    data = _lme_data(tmp_path)
    paid = bench_longmemeval.run(_ctx(tmp_path, "runs", FakeClient("reader", "x"), FakeClient("judge", "no")),
                                 data_path=data, official=FakeLongMemEval())
    screen = bench_longmemeval.run(_ctx(tmp_path, "screens", FakeClient("reader", "x"), FakeClient("judge", "no"),
                                        retrieval_only=True), data_path=data, official=FakeLongMemEval())
    p, s = _rows(paid), _rows(screen)
    assert sorted(p) == sorted(s) == ["q1", "q2"]
    assert {q: p[q]["prompt_sha256"] for q in p} == {q: s[q]["prompt_sha256"] for q in s}


def test_the_manifest_records_no_cache_only_when_the_run_bypassed_the_cache(tmp_path):
    data = _lme_data(tmp_path)
    fresh = bench_longmemeval.run(_ctx(tmp_path, "fresh", FakeClient("reader", "x"), FakeClient("judge", "no"),
                                       no_cache=True), data_path=data, official=FakeLongMemEval())
    cached = bench_longmemeval.run(_ctx(tmp_path, "cached", FakeClient("reader", "x"), FakeClient("judge", "no")),
                                   data_path=data, official=FakeLongMemEval())
    assert json.loads((fresh / "manifest.json").read_text())["config"]["no_cache"] is True
    # A run that may use the cache stores no key, so the configs of earlier runs, and their resumes, are unchanged.
    assert "no_cache" not in json.loads((cached / "manifest.json").read_text())["config"]


def test_the_cli_hands_no_cache_to_the_run(tmp_path, monkeypatch):
    from telarch_evals import cli

    seen: list[Context] = []

    def fake_run(ctx, **kw):
        seen.append(ctx)
        out = tmp_path / f"run{len(seen)}"
        out.mkdir()
        (out / "summary.json").write_text(json.dumps({"benchmark": "longmemeval", "rows": 0, "errors": 0,
                                                      "complete": True}))
        return out

    monkeypatch.setattr(bench_longmemeval, "run", fake_run)
    monkeypatch.setenv("TELARCH_EVALS_HOME", str(tmp_path / "home"))
    args = ["longmemeval", "--api-key-env", "NONE", "--runs-dir", str(tmp_path / "runs")]
    assert cli.main(args + ["--no-cache"]) == 0
    assert cli.main(args) == 0
    assert [c.no_cache for c in seen] == [True, False]
    assert seen[0].reader.cache is None and seen[1].reader.cache is not None
