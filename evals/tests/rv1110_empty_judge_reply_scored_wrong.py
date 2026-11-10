"""severity: medium
finding: an empty judge reply (content "", finish_reason "length") is scored as a wrong answer with status ok

Location: evals/telarch_evals/llm.py:331-332 (`if text is None: raise LLMError("... returned no text ...")`: the
guard misses the empty string), evals/telarch_evals/bench_longmemeval.py:295-297 and
bench_memoryagentbench.py:291-293 (the judge is called with max_tokens=10 and the label is
`"yes" in verdict.text.strip().lower()`).

Effect: a reasoning model spends a 10-token completion budget on reasoning and returns `"content": ""` with
`finish_reason: "length"`; llm.py:300-301 sends exactly that budget as max_completion_tokens for o-series and
gpt-5 names (gpt-5, gpt-5-mini and gpt-5-nano are in the price table), and gateway models that reason (the suite's own test_reported_cost
fixture reports 220 reasoning tokens of 225) do the same. The harness then records `label: false` for a question
the judge never ruled on, with status ok: the run is `complete: true`, `errors: 0`, and its accuracy counts every
such row as wrong. A failed judge call is supposed to be an error row that a resume retries
("never counted as a wrong answer", runlog.py:195), which is what happens for `"content": null`.
"""
import json
from pathlib import Path

import pytest
import telarch_evals
from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import ChatClient, Meter
from telarch_evals.memory import NoMemory      # needs no tokenizer; what is retrieved does not matter here

EVALS = Path(__file__).resolve().parents[1]


class FakeLongMemEval:
    reader_template_cot = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
    reader_template_plain = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer:"

    def anscheck_prompt(self, task, question, answer, response, abstention=False):
        return f"gold={answer} response={response}"

    def judge_model(self, name):
        return name


def client(meter, model, content, finish_reason="stop"):
    c = ChatClient(model, api_key_env="TELARCH_TEST_KEY", cache=None, meter=meter, retries=0)
    c._post = lambda body: {"model": model, "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                                      "completion_tokens_details": {"reasoning_tokens": 10 if not content else 0}}}
    return c


def run(tmp_path, monkeypatch, judge_content, finish_reason="stop") -> dict:
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    session = [{"role": "user", "content": "I adopted a beagle named Biscuit.", "has_answer": True},
               {"role": "assistant", "content": "Congratulations!"}]
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "lme.json"
    data.write_text(json.dumps([{
        "question_id": "q1", "question_type": "single-session-user", "question": "What breed is Biscuit?",
        "answer": "beagle", "question_date": "2023/06/01 (Thu) 10:00", "haystack_session_ids": ["s1"],
        "haystack_dates": ["2023/05/01 (Mon) 09:00"], "haystack_sessions": [session], "answer_session_ids": ["s1"]}]),
        encoding="utf-8")
    meter = Meter()
    ctx = Context(runs_root=tmp_path / "runs", workdir=tmp_path / "work", system=NoMemory(),
                  reader=client(meter, "gpt-4o-mini", "Biscuit is a beagle."),
                  judge=client(meter, "gpt-5-mini", judge_content, finish_reason), meter=meter, workers=1, resume=None)
    out = bench_longmemeval.run(ctx, data_path=data, official=FakeLongMemEval())
    return json.loads((out / "summary.json").read_text())


def test_control_a_yes_is_scored_right_and_a_null_reply_is_an_error_row(tmp_path, monkeypatch):
    assert Path(telarch_evals.__file__).resolve().is_relative_to(EVALS)
    summary = run(tmp_path / "yes", monkeypatch, "yes")
    assert summary["complete"] and summary["rows"] == 1 and summary["overall_accuracy"] == 1.0
    summary = run(tmp_path / "null", monkeypatch, None, "length")
    assert not summary["complete"] and summary["rows"] == 0 and summary["errors"] == 1
    assert "returned no text" in summary["first_errors"][0]


@pytest.mark.parametrize("content", ["", "  \n"], ids=["empty string", "whitespace"])
def test_an_empty_judge_reply_is_not_a_verdict(tmp_path, monkeypatch, content):
    summary = run(tmp_path, monkeypatch, content, "length")
    assert summary["rows"] == 0 and summary["errors"] == 1 and not summary["complete"], (
        f"the judge returned {content!r} (finish_reason=length); the summary says rows={summary['rows']}, "
        f"errors={summary['errors']}, complete={summary['complete']}, overall_accuracy={summary['overall_accuracy']}")
