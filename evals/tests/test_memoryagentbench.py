"""Offline tests for the MemoryAgentBench module, with a stand-in for the benchmark's own code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from telarch_evals import bench_memoryagentbench as mab
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import FullContextMemory, TelarchMemory
from telarch_evals.official import MemoryAgentBenchOfficial


class WordEncoding:
    """A tokenizer whose tokens are the space-separated words, so decode(encode(s)) == s."""

    def encode(self, text, disallowed_special=()):
        return text.split(" ")

    def decode(self, ids):
        return " ".join(ids)


class FakeMAB:
    system_message = "SYS"
    encoding = WordEncoding()
    summary_score = staticmethod(MemoryAgentBenchOfficial.summary_score)

    def tokens(self, text):
        return len(self.encoding.encode(text))

    def get_template(self, source, kind, agent):
        if kind == "memorize":
            return "MEM {time_stamp}: {context}"
        return "Now Answer the Question: {question}" if agent == "rag_agent" else "LC Q: {question}"

    def chunk(self, text, chunk_size=4096):
        words = text.split(" ")
        return [" ".join(words[i:i + 60]) for i in range(0, len(words), 60)]

    def sent_tokenize(self, text):
        return [s for s in re.split(r"(?<=\.) ", text) if s]

    def post_process(self, output, answer, config):
        golds = [g for a in answer for g in (a if isinstance(a, list) else [a])]  # nested, as calculate_metrics allows
        hit = any(g.lower() in output["output"].lower() for g in golds)
        return {"substring_exact_match": hit, "exact_match": False}, {"parsed_output": output["output"]}

    def anscheck_prompt(self, qtype, question, answer, response, abstention=False):
        return f"gold={answer} response={response} type={qtype}"

    def summary_prompts(self, output, keypoints, answer):
        return [f"fluency|{output}", f"recall|{output}", f"precision|{output}"]

    def parse_json(self, text):
        return json.loads(text)


class FakeClient:
    def __init__(self, model, reply):
        self.model, self.reply, self.calls = model, reply, []

    def describe(self):
        return {"model": self.model}

    def derive(self, model, **_kwargs):
        return FakeClient(model, self.reply)

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        text = self.reply(messages)
        return SimpleNamespace(text=text, model=self.model, usage={"prompt_tokens": 5, "completion_tokens": 1})


def _text(messages):
    return messages if isinstance(messages, str) else " ".join(m["content"] for m in messages)


def _context(needle: str, words: int = 600) -> str:
    filler = " ".join(f"Filler sentence {i} about weather." for i in range(words // 5))
    return f"{filler} {needle} {filler}"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    ar = [
        {"context": _context("The capital of Zubrowka is Lutz."), "questions": ["What is the capital of Zubrowka?"],
         "answers": [["Lutz"]], "metadata": {"source": "ruler_qa1_197K"}},
        {"context": _context("I adopted a beagle named Biscuit."),
         "questions": ["Current Date: 2023/05/27,\n\n Now Answer the Question: What breed is Biscuit?",
                       "Current Date: 2023/05/27,\n\n Now Answer the Question: What is my cat called?"],
         "answers": [["beagle"], ["You did not mention a cat."]],
         "metadata": {"source": "longmemeval_s*", "question_types": ["single-session-user", "single-session-user"],
                      "question_ids": ["q1", "q2_abs"]}},
    ]
    lru = [{"context": _context("Jennifer marries Mina Loris."), "questions": ["Write a summary."],
            "answers": [["Jennifer marries Loris and doubts him."]],
            "metadata": {"source": "infbench_sum_eng_shots2", "keypoints": ["marries", "doubts"]}}]
    (tmp_path / "Accurate_Retrieval.json").write_text(json.dumps(ar))
    (tmp_path / "Long_Range_Understanding.json").write_text(json.dumps(lru))
    return tmp_path


def _reader(messages):
    text = _text(messages)
    if "Zubrowka?" in text:
        return "Lutz" if "Lutz is" in text or "is Lutz" in text else "unknown"
    if "Biscuit?" in text:
        return "a beagle" if "beagle named" in text else "unknown"
    if "Write a summary." in text:
        return "Jennifer marries Loris."
    return "I do not know."


def _judge(messages):
    text = _text(messages)
    if text.startswith("fluency|"):
        return '{"fluency": 1}'
    if text.startswith("recall|"):
        return '{"recall": 1}'
    if text.startswith("precision|"):
        return '{"precision": 1, "sentence_count": 1}'
    return "yes" if "response=a beagle" in text or "response=I do not know" in text else "no"


def _ctx(tmp_path: Path, system) -> Context:
    return Context(runs_root=tmp_path / "runs", workdir=tmp_path / "work", system=system,
                   reader=FakeClient("reader", _reader), judge=FakeClient("judge", _judge), meter=Meter(), workers=2)


DATASETS = ["ruler_qa1_197K", "longmemeval_s*", "infbench_sum_eng_shots2"]


def test_qa_items_pairing_and_retrieval_query():
    many = {"questions": ["a?", "b?"], "answers": [["1"], ["2"]],
            "metadata": {"question_types": ["t1", "t2"], "qa_pair_ids": ["x", "y"]}}
    items = mab.qa_items(many)
    assert [i["answer"] for i in items] == [["1"], ["2"]] and items[1]["question_types"] == "t2"
    one = {"questions": ["summarize"], "answers": [["ref"]], "metadata": {"qa_pair_ids": ["s0"]}}
    assert mab.qa_items(one) == [{"question": "summarize", "answer": [["ref"]], "qa_pair_ids": "s0"}]
    assert mab.retrieval_query("Rules. Now Answer the Question: Who? \nAnswer:", "raw") == "Who? \nAnswer:"
    assert mab.retrieval_query("Label this: text", "text") == "text"


def test_passages_are_verbatim_spans_within_the_limit(monkeypatch):
    monkeypatch.setattr(mab, "PASSAGE_TOKENS", 12)
    official = FakeMAB()
    chunk = "One two three four. Five six seven eight nine. " + " ".join(f"w{i}" for i in range(30)) + ". End here."
    parts = mab.passages(official, chunk)
    assert all(official.tokens(p) <= 12 for p in parts)
    assert all(p in chunk for p in parts) and "".join(parts).replace(" ", "") == chunk.replace(" ", "")


def test_long_context_keeps_the_newest_tokens():
    official = FakeMAB()
    words = [f"w{i}" for i in range(130_000)]
    chunks = [" ".join(words[:65_000]), " ".join(words[65_000:])]
    # context_max_length 200,000: the agent's own limit applies, 128,000 - 4,000 - 0 tokens.
    ds = mab.Dataset("ruler_qa1_197K", "Accurate_Retrieval", "AR", "SH-QA", "substring_exact_match", 200_000, 0)
    context = mab.long_context(official, chunks, ds)
    assert official.tokens(context) == 124_000 and context.endswith("w129999")
    # A limit far above context_max_length + 50,000 leaves the context whole, as agent.py does.
    roomy = mab.Dataset("x", "Accurate_Retrieval", "AR", "SH-QA", "substring_exact_match", 10, 0)
    assert mab.long_context(official, ["a b", "c"], roomy) == f"MEM {mab.TIME_STAMP}: a b\nMEM {mab.TIME_STAMP}: c"


def test_telarch_run_scores_all_three_metric_kinds(tmp_path: Path, data_dir: Path):
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work", budget=400))
    out = mab.run(ctx, datasets=DATASETS, data_dir=data_dir, official=FakeMAB())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["complete"] and summary["rows"] == 4
    # The stand-in judge finds 1 of 2 key points with full precision: F1 = 2 * 0.5 * 1 / 1.5.
    assert summary["columns"] == {"SH-QA": 100.0, "LME(S*)": 100.0, "Summ.": 66.67}
    assert summary["overall"] is None and summary["categories"]["SF"] is None
    first = _text(ctx.reader.calls[0])
    assert first.startswith("SYS ") and "Memory 1:\n" in first and "Now Answer the Question:" in first


def test_full_context_run_uses_the_long_context_agent(tmp_path: Path, data_dir: Path):
    ctx = _ctx(tmp_path, FullContextMemory())
    out = mab.run(ctx, datasets=["ruler_qa1_197K"], data_dir=data_dir, official=FakeMAB())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["columns"] == {"SH-QA": 100.0}
    prompt = ctx.reader.calls[0][1]["content"]
    assert prompt.startswith(f"MEM {mab.TIME_STAMP}: ") and prompt.endswith("LC Q: What is the capital of Zubrowka?")
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["config"]["memory"]["protocol"] == "MemoryAgentBench long-context agent"


def test_summary_parse_failure_is_unscored_not_zero(tmp_path: Path, data_dir: Path):
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"))
    ctx.judge = FakeClient("judge", lambda m: "not json")
    out = mab.run(ctx, datasets=["infbench_sum_eng_shots2"], data_dir=data_dir, official=FakeMAB())
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rows"] == 1 and summary["by_dataset"] == {} and summary["columns"] == {}


@pytest.mark.network
def test_memoryagentbench_official_code_loads():
    official = MemoryAgentBenchOfficial()
    for ds in mab.DATASETS:
        for agent in ("rag_agent", "Long_context_agent"):
            assert "{question}" in official.get_template(ds.source, "query", agent)
        assert "{context}" in official.get_template(ds.source, "memorize", "Long_context_agent")
    chunks = official.chunk(" ".join(f"Sentence {i} is here." for i in range(5000)), chunk_size=4096)
    assert len(chunks) > 1 and max(official.tokens(c) for c in chunks) <= 4096
    scores, _ = official.post_process({"output": "label: 43"}, ["43"], {"sub_dataset": "icl_nlu_8296shot_balance"})
    assert scores["substring_exact_match"] and not scores["exact_match"]
    assert "Answer yes or no only" in official.anscheck_prompt("multi-session", "q", ["a"], "r")
