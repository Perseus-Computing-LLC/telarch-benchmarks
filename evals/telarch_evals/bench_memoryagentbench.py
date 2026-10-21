"""MemoryAgentBench (Hu, Wang & McAuley, ICLR 2026): long contexts fed to a memory agent in chunks.

The fourteen datasets of the paper's Table 3, in four competencies:
- accurate retrieval: SH-QA, MH-QA, LongMemEval (S*) and EventQA
- test-time learning: five intent and question classification sets, averaged as MCC, and movie
  recommendation
- long-range understanding: ∞Bench-Sum and Detective QA
- selective forgetting: FactConsolidation single-hop and multi-hop

Each context is cut by the benchmark's own chunk_text_into_sentences into 4,096-token chunks, and
every prompt is the benchmark's template. The scores are the benchmark's metrics:
- SubEM for the QA, classification and FactConsolidation sets
- EventQA recall
- Recall@5 for recommendation
- the LongMemEval judge, gpt-4o
- the ∞Bench-Sum judge: gpt-4o-2024-05-13, F1 times fluency

A category score is the mean of its columns and the overall score is the mean of the four
categories, as in the paper. The reader runs at the agents' temperature, 0.7.

Systems:
- telarch: each chunk is split into passages of at most 512 tokens and stored in Kibisis. The
  reader sees the passages Harpe selects, labelled "Memory i:" as in the benchmark's RAG agents,
  then the benchmark's RAG query.
- full-context: the benchmark's long-context agent. Every chunk is wrapped in its memorize
  template and the whole is cut to the newest 128,000 - 4,000 - generation-limit tokens, then the
  long-context query follows.
- none: the RAG query alone.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import Context, base_config, common_summary, screen_row, screen_summary
from .memory import Unit
from .official import MemoryAgentBenchOfficial
from .runlog import RunLog, execute
from .sources import MAB_CODE, fetch, record

CHUNK_TOKENS = 4096                 # chunk_size in the data configs of the main experiments
PASSAGE_TOKENS = 512                # telarch only: passages stored per chunk
TIME_STAMP = "2025-07-07 00:00:00"  # the memorize templates take the wall clock; fixed so prompts repeat
LONG_CONTEXT_LIMIT = 128_000        # Long_context_agent_gpt-4o-mini.yaml: input_length_limit
LONG_CONTEXT_BUFFER = 4_000         # ... buffer_length
READER_TEMPERATURE = 0.7            # temperature in every agent config
OFFICIAL_JUDGE = "gpt-4o-2024-08-06"  # longmem_qa_evaluate.py asks for "gpt-4o", which named this snapshot
SUMMARY_JUDGE = "gpt-4o-2024-05-13"   # summarization_evaluate.py, with these settings:
SUMMARY_JUDGE_SETTINGS = {"temperature": 0.1, "max_tokens": 4096, "extra": {"top_p": 0.9, "seed": 42}}


@dataclass(frozen=True)
class Dataset:
    source: str                 # metadata.source in the Hugging Face dataset
    split: str
    category: str
    column: str                 # column of the paper's Table 3
    metric: str                 # the score that column reports
    context_max_length: int     # from configs/data_conf at the pinned commit
    generation_max_length: int


_ICL = ("Test_Time_Learning", "TTL", "MCC", "substring_exact_match", 131072, 20)
DATASETS = (
    Dataset("ruler_qa1_197K", "Accurate_Retrieval", "AR", "SH-QA", "substring_exact_match", 220000, 50),
    Dataset("ruler_qa2_421K", "Accurate_Retrieval", "AR", "MH-QA", "substring_exact_match", 524288, 50),
    Dataset("longmemeval_s*", "Accurate_Retrieval", "AR", "LME(S*)", "llm_judge", 400000, 50),
    Dataset("eventqa_full", "Accurate_Retrieval", "AR", "EventQA", "eventqa_recall", 800000, 40),
    Dataset("icl_banking77_5900shot_balance", *_ICL),
    Dataset("icl_clinic150_7050shot_balance", *_ICL),
    Dataset("icl_nlu_8296shot_balance", *_ICL),
    Dataset("icl_trec_coarse_6600shot_balance", *_ICL),
    Dataset("icl_trec_fine_6400shot_balance", *_ICL),
    Dataset("recsys_redial_full", "Test_Time_Learning", "TTL", "Recom.", "recsys_recall@5", 1480000, 300),
    Dataset("infbench_sum_eng_shots2", "Long_Range_Understanding", "LRU", "Summ.", "summary_f1", 1200000, 1200),
    Dataset("detective_qa", "Long_Range_Understanding", "LRU", "DetQA", "substring_exact_match", 200000, 2000),
    Dataset("factconsolidation_sh_262k", "Conflict_Resolution", "SF", "FC-SH", "substring_exact_match", 300000, 10),
    Dataset("factconsolidation_mh_262k", "Conflict_Resolution", "SF", "FC-MH", "substring_exact_match", 300000, 10),
)
BY_SOURCE = {d.source: d for d in DATASETS}
CATEGORIES = {"AR": ("SH-QA", "MH-QA", "LME(S*)", "EventQA"), "TTL": ("MCC", "Recom."),
              "LRU": ("Summ.", "DetQA"), "SF": ("FC-SH", "FC-MH")}
# The patterns agent.py's _extract_retrieval_query uses to pull the question out of a RAG query.
_RETRIEVAL_QUERY = (re.compile(r"Now Answer the Question:\s*(.*)", re.DOTALL),
                    re.compile(r"Here is the conversation:\s*(.*)", re.DOTALL))


def load_rows(split: str, data_dir: Path | None) -> list[dict]:
    """Rows of one split: the pinned parquet file, or <split>.json / .parquet under data_dir."""
    if data_dir:
        local = Path(data_dir) / f"{split}.json"
        if local.exists():
            return json.loads(local.read_text("utf-8"))
        path = Path(data_dir) / f"{split}.parquet"
    else:
        path = fetch(f"memoryagentbench/{split}.parquet")
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def qa_items(row: dict) -> list[dict]:
    """Question, answer and per-question metadata, paired as conversation_creator.py pairs them."""
    questions, answers = _as_list(row["questions"]), _as_list(row["answers"])
    meta = row.get("metadata") or {}
    if len(questions) > 1 and len(answers) > 1:
        pairs = list(zip(questions, answers))
    else:
        pairs = [(questions[0] if questions else "", answers)]
    items = []
    for i, (question, answer) in enumerate(pairs):
        item: dict[str, Any] = {"question": question, "answer": answer}
        for name in ("question_dates", "question_types", "question_ids", "previous_events", "qa_pair_ids"):
            value = row.get(name) or meta.get(name)
            if value is not None:
                item[name] = value[i] if isinstance(value, list) and i < len(value) else value
        items.append(item)
    return items


def memorize(template: str, chunk: str) -> str:
    return template.format(context=chunk, **({"time_stamp": TIME_STAMP} if "{time_stamp}" in template else {}))


def retrieval_query(rag_query: str, question: str) -> str:
    for pattern in _RETRIEVAL_QUERY:
        match = pattern.search(rag_query)
        if match:
            return "".join(match.groups())
    return question  # agent.py would search with the whole prompt; the question alone is the better query


def passages(official: MemoryAgentBenchOfficial, chunk: str) -> list[str]:
    """Split a chunk into spans of at most PASSAGE_TOKENS tokens on sentence, then line, then token edges."""
    def pieces() -> Iterator[str]:
        for sentence in official.sent_tokenize(chunk):
            if official.tokens(sentence) <= PASSAGE_TOKENS:
                yield sentence
                continue
            for line in sentence.split("\n"):
                if official.tokens(line) <= PASSAGE_TOKENS:
                    yield line
                    continue
                ids = official.encoding.encode(line, disallowed_special=())
                for start in range(0, len(ids), PASSAGE_TOKENS):
                    yield official.encoding.decode(ids[start:start + PASSAGE_TOKENS])

    out: list[str] = []
    first, last, size, pos = -1, -1, 0, 0
    for piece in pieces():
        if not piece.strip():
            continue
        start = chunk.find(piece, pos)
        if start < 0:  # not a verbatim span (should not happen): keep it as its own passage
            out.append(piece)
            continue
        n = official.tokens(piece)
        if first >= 0 and size + n > PASSAGE_TOKENS:
            out.append(chunk[first:last])
            first, size = -1, 0
        if first < 0:
            first = start
        last, size, pos = start + len(piece), size + n, start + len(piece)
    if first >= 0:
        out.append(chunk[first:last])
    return out


def long_context(official: MemoryAgentBenchOfficial, chunks: list[str], ds: Dataset) -> str:
    """The long-context agent's memory: every memorized chunk, cut to its newest tokens as agent.py cuts it."""
    template = official.get_template(ds.source, "memorize", "Long_context_agent")
    context = "\n".join(memorize(template, chunk) for chunk in chunks).strip()
    limit = LONG_CONTEXT_LIMIT - LONG_CONTEXT_BUFFER - ds.generation_max_length
    if limit <= ds.context_max_length + 50000:
        for keep in (ds.context_max_length, limit):
            ids = official.encoding.encode(context, disallowed_special=())
            if len(ids) > keep:
                context = official.encoding.decode(ids[-keep:])
    return context


def plan(selected: list[Dataset], data_dir: Path | None, per_dataset: int | None,
         rows: int | None) -> list[tuple[Dataset, int, dict, list[tuple[int, dict]]]]:
    loaded: dict[str, list[dict]] = {}
    out = []
    for ds in selected:
        if ds.split not in loaded:
            loaded[ds.split] = load_rows(ds.split, data_dir)
        matching = [r for r in loaded[ds.split] if (r.get("metadata") or {}).get("source") == ds.source]
        left = per_dataset
        for ri, row in enumerate(matching[:rows] if rows else matching):
            items = list(enumerate(qa_items(row)))
            if left is not None:
                items, left = items[:left], left - len(items[:left])
            if items:
                out.append((ds, ri, row, items))
            if left == 0:
                break
    return out


def _parsed(official: MemoryAgentBenchOfficial, text: str) -> dict | None:
    try:
        value = official.parse_json(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def run(ctx: Context, *, datasets: list[str] | None = None, per_dataset: int | None = None, rows: int | None = None,
        data_dir: Path | None = None, official: MemoryAgentBenchOfficial | None = None) -> Path:
    official = official or MemoryAgentBenchOfficial()
    unknown = sorted(set(datasets or []) - set(BY_SOURCE))
    if unknown:
        raise SystemExit(f"unknown MemoryAgentBench datasets: {', '.join(unknown)}")
    selected = [BY_SOURCE[s] for s in datasets] if datasets else list(DATASETS)
    full = ctx.system.name == "full-context"
    summary_judge = ctx.judge.derive(SUMMARY_JUDGE, **SUMMARY_JUDGE_SETTINGS)
    extra: dict[str, Any] = {"benchmark": "memoryagentbench", "datasets": [d.source for d in selected],
                             "per_dataset": per_dataset, "rows": rows, "chunk_tokens": CHUNK_TOKENS,
                             "time_stamp": TIME_STAMP, "summary_judge": summary_judge.describe()}
    if ctx.system.name == "telarch":
        extra["passage_tokens"] = PASSAGE_TOKENS
    config = base_config(ctx, extra)
    if full:  # the unit budget of FullContextMemory does not apply here
        config["memory"] = {"system": "full-context", "protocol": "MemoryAgentBench long-context agent",
                            "input_length_limit": LONG_CONTEXT_LIMIT, "buffer_length": LONG_CONTEXT_BUFFER,
                            "tokenizer": "o200k_base (gpt-4o-mini)"}
    splits = sorted({d.split for d in selected})
    sources = ([] if data_dir else [record(f"memoryagentbench/{s}.parquet") for s in splits]) + [record(k) for k in MAB_CODE]
    log = RunLog(ctx.runs_root, "memoryagentbench", config, sources, ctx.resume)
    work_plan = plan(selected, data_dir, per_dataset, rows)
    expected = sum(len(items) for *_, items in work_plan)
    system_turn = {"role": "system", "content": official.system_message}

    def jobs() -> Iterator[tuple[str, Any]]:
        for ds, ri, row, items in work_plan:
            todo = [(qi, it) for qi, it in items if f"{ds.source}/{ri}/{qi}" not in log.done]
            if not todo:
                continue
            chunks = official.chunk(row["context"], chunk_size=CHUNK_TOKENS)
            keypoints = (row.get("metadata") or {}).get("keypoints") or []
            if full:
                context = long_context(official, chunks, ds)
                memory = {"chunks": len(chunks), "context_tokens": official.tokens(context)}
                template = official.get_template(ds.source, "query", "Long_context_agent")
                for qi, it in todo:
                    user = {"role": "user", "content": context + "\n" + template.format(**it)}
                    yield f"{ds.source}/{ri}/{qi}", (ds, ri, qi, it, keypoints, [system_turn, user], memory)
                continue
            units = [] if ctx.system.name == "none" else [
                Unit(uid=f"{ci}:{pi}", session=f"chunk-{ci}", order=(ci, pi), date="", text=text, rendered=text)
                for ci, chunk in enumerate(chunks) for pi, text in enumerate(passages(official, chunk))]
            history_id = f"{ds.source}/{ri}"
            ingest = ctx.system.open(history_id, units)
            try:
                template = official.get_template(ds.source, "query", "rag_agent")
                for qi, it in todo:
                    query = template.format(**it)
                    recall = ctx.system.recall(history_id, retrieval_query(query, str(it["question"])))
                    memories = "\n".join(f"Memory {i + 1}:\n{u.rendered}\n" for i, u in enumerate(recall.units))
                    user = {"role": "user", "content": memories + "\n" + query}
                    memory = {**ingest, **recall.stats, "chunks": len(chunks)}
                    yield f"{ds.source}/{ri}/{qi}", (ds, ri, qi, it, keypoints, [system_turn, user], memory)
            finally:
                ctx.system.close(history_id)

    def work(job_id: str, payload: Any) -> dict:
        ds, ri, qi, it, keypoints, messages, memory = payload
        reply = ctx.reader.complete(messages, max_tokens=ds.generation_max_length)
        out = {"id": job_id, "dataset": ds.source, "column": ds.column, "category": ds.category, "row": ri,
               "index": qi, "question": it["question"], "answer": it["answer"], "output": reply.text,
               "reader_model": reply.model, "reader_usage": reply.usage, "memory": memory}
        if ds.metric == "llm_judge":
            prompt = official.anscheck_prompt(it.get("question_types"), it["question"], it["answer"], reply.text,
                                              abstention="_abs" in str(it.get("question_ids", "")))
            verdict = ctx.judge.complete([{"role": "user", "content": prompt}], max_tokens=10)
            out.update(judge_reply=verdict.text, judge_model=verdict.model,
                       scores={"llm_judge": float("yes" in verdict.text.strip().lower())})
        elif ds.metric == "summary_f1":
            replies = [summary_judge.complete([{"role": "user", "content": p}])
                       for p in official.summary_prompts(reply.text, keypoints, it["answer"])]
            parsed = [_parsed(official, r.text) for r in replies]
            out.update(judge_replies=[r.text for r in replies], judge_model=replies[0].model)
            fluency, recall, precision = parsed
            try:
                score = (official.summary_score(fluency, recall, precision, keypoints)
                         if fluency and recall and precision else None)
            except (KeyError, TypeError, ZeroDivisionError):
                score = None
            # summarization_evaluate.py leaves out summaries whose judge replies do not parse.
            out["scores"] = {"summary_f1": None} if score is None else {f"summary_{k}": v for k, v in score.items()}
        else:
            metrics, info = official.post_process({"output": reply.text}, it["answer"], {"sub_dataset": ds.source})
            out.update(scores={k: float(v) for k, v in metrics.items()}, parsed_output=info.get("parsed_output"))
        return out

    if ctx.retrieval_only:  # a $0 screen: memory and evidence recall only, no reader or judge calls
        def screen(job_id: str, payload: Any) -> dict:
            ds, ri, qi, it, keypoints, messages, memory = payload
            return screen_row(job_id, ds.source, memory, None, messages)

        stop = execute(log, jobs(), screen, workers=ctx.workers, meter=ctx.meter, max_usd=None)
        return log.finish(screen_summary(log.rows(), stop, expected, "memoryagentbench", ()))

    stop = execute(log, jobs(), work, workers=ctx.workers, meter=ctx.meter, max_usd=ctx.max_usd)
    return log.finish(summarize(log.rows(), ctx.meter, stop, expected))


def _pct(values: list[float]) -> float | None:
    return round(100 * sum(values) / len(values), 2) if values else None


def summarize(rows: list[dict], meter=None, stop: str | None = None, expected: int | None = None) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    primary: dict[str, list[float]] = defaultdict(list)
    every: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    unscored: dict[str, int] = defaultdict(int)
    for r in ok:
        ds = BY_SOURCE.get(r["dataset"])
        value = r["scores"].get(ds.metric) if ds else None
        if value is None:
            unscored[r["dataset"]] += 1
        else:
            primary[r["dataset"]].append(float(value))
        for key, v in r["scores"].items():
            if isinstance(v, int | float):
                every[r["dataset"]][key].append(float(v))
    per_dataset = {s: {"column": BY_SOURCE[s].column, "metric": BY_SOURCE[s].metric, "score": _pct(v), "n": len(v),
                       "unscored": unscored.get(s, 0), "metrics": {k: _pct(x) for k, x in sorted(every[s].items())}}
                   for s, v in sorted(primary.items())}
    columns: dict[str, float] = {}
    for col in (c for cols in CATEGORIES.values() for c in cols):
        scores = [d["score"] for d in per_dataset.values() if d["column"] == col and d["score"] is not None]
        if scores:
            columns[col] = round(sum(scores) / len(scores), 2)
    categories = {cat: (round(sum(columns[c] for c in cols) / len(cols), 2) if all(c in columns for c in cols) else None)
                  for cat, cols in CATEGORIES.items()}
    complete_categories = [v for v in categories.values() if v is not None]
    summary = common_summary(rows, meter, stop, expected if expected is not None else len(rows))
    summary.update({
        "benchmark": "memoryagentbench",
        "overall": round(sum(complete_categories) / 4, 2) if len(complete_categories) == 4 else None,
        "categories": categories,
        "columns": columns,
        "by_dataset": per_dataset,
        "scale": "percent, as in the paper's Table 3",
    })
    return summary
