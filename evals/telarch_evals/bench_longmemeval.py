"""LongMemEval (Wu et al., ICLR 2025): 500 questions, each over its own chat history.

Reader prompt: the official template from run_generation.py (chain-of-thought by default), with the
history rendered in the official "nl" session format. Judge: the official get_anscheck_prompt from
evaluate_qa.py with gpt-4o-2024-08-06, temperature 0, max_tokens 10, label = "yes" in the reply.
judgments.jsonl rows carry `question_id` and `autoeval_label`, so LongMemEval's own
print_qa_metrics.py can score a run directly; predictions.jsonl holds the hypotheses in the
format evaluate_qa.py reads.
"""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .common import Context, base_config, common_summary, iter_json_array, mean
from .memory import Unit
from .official import LongMemEvalOfficial
from .runlog import RunLog, execute
from .sources import LONGMEMEVAL_SPLITS, fetch, record

OFFICIAL_JUDGE = "gpt-4o-2024-08-06"


def units_for(entry: dict) -> list[Unit]:
    """One unit per round: a user turn and the assistant turns that follow it."""
    answer_sessions = set(entry.get("answer_session_ids") or [])
    units: list[Unit] = []
    for si, (sid, date, session) in enumerate(zip(entry["haystack_session_ids"], entry["haystack_dates"],
                                                   entry["haystack_sessions"])):
        rounds: list[list[dict]] = []
        for turn in session:
            if turn.get("role") == "user" or not rounds:
                rounds.append([])
            rounds[-1].append(turn)
        for ri, turns in enumerate(rounds):
            text = "\n".join(f"{t.get('role', 'user')}: {str(t.get('content', '')).strip()}" for t in turns)
            units.append(Unit(
                uid=f"{si}:{ri}", session=str(sid), order=(str(date), si, ri), date=str(date), text=text,
                rendered=f"[{date}]\n{text}",
                turns=[{"role": t.get("role", "user"), "content": str(t.get("content", ""))} for t in turns],
                meta={"has_answer": any(t.get("has_answer") for t in turns), "answer_session": sid in answer_sessions}))
    return units


def render_history(units: list[Unit]) -> str:
    """The official nl rendering: selected turns grouped into their sessions, sessions by date."""
    sessions: OrderedDict[tuple, tuple[str, list[dict]]] = OrderedDict()
    for unit in sorted(units, key=lambda u: u.order):
        sessions.setdefault(unit.order[:2], (unit.date, []))[1].extend(unit.turns)
    history = ""
    for i, (date, turns) in enumerate(sessions.values(), 1):
        sess = "".join("\n\n{}: {}".format(t["role"], t["content"].strip()) for t in turns)
        history += f"\n### Session {i}:\nSession Date: {date}\nSession Content:\n{sess}\n"
    return history


def select(entries: Iterator[dict], *, limit: int | None, per_type: int | None, ids: set[str] | None,
           types: set[str] | None) -> Iterator[dict]:
    counts: dict[str, int] = defaultdict(int)
    taken = 0
    for entry in entries:
        if ids is not None and entry["question_id"] not in ids:
            continue
        if types is not None and entry["question_type"] not in types:
            continue
        if per_type is not None:
            if counts[entry["question_type"]] >= per_type:
                continue
            counts[entry["question_type"]] += 1
        yield entry
        taken += 1
        if limit is not None and taken >= limit:
            return


def evidence(units: list[Unit], chosen: list[Unit], entry: dict) -> dict[str, Any]:
    answer_sessions = {u.session for u in units if u.meta.get("answer_session")}
    answer_turns = {u.uid for u in units if u.meta.get("has_answer")}
    chosen_sessions = {u.session for u in chosen}
    chosen_ids = {u.uid for u in chosen}
    out: dict[str, Any] = {"answer_sessions": len(answer_sessions), "answer_rounds": len(answer_turns)}
    if answer_sessions:
        out["session_recall"] = round(len(answer_sessions & chosen_sessions) / len(answer_sessions), 4)
    if answer_turns:
        out["round_recall"] = round(len(answer_turns & chosen_ids) / len(answer_turns), 4)
    return out


def run(ctx: Context, *, split: str = "s", cot: bool = True, limit: int | None = None,
        per_type: int | None = None, ids: set[str] | None = None, types: set[str] | None = None,
        data_path: Path | None = None, official: LongMemEvalOfficial | None = None) -> Path:
    official = official or LongMemEvalOfficial()
    path = Path(data_path) if data_path else fetch(LONGMEMEVAL_SPLITS[split])
    template = official.reader_template_cot if cot else official.reader_template_plain
    judge_model = official.judge_model(ctx.judge.model)
    config = base_config(ctx, {"benchmark": "longmemeval", "split": split, "cot": cot, "limit": limit,
                               "per_type": per_type, "ids": sorted(ids) if ids else None,
                               "types": sorted(types) if types else None,
                               "dataset": str(path.name) if data_path else LONGMEMEVAL_SPLITS[split]})
    sources = ([] if data_path else [record(LONGMEMEVAL_SPLITS[split])]) + [
        record("longmemeval/evaluate_qa.py"), record("longmemeval/run_generation.py")]
    log = RunLog(ctx.runs_root, "longmemeval", config, sources, ctx.resume)
    selected = [e["question_id"] for e in select(iter_json_array(path), limit=limit, per_type=per_type,
                                                ids=ids, types=types)]
    expected = len(selected)

    def jobs() -> Iterator[tuple[str, Any]]:
        wanted = set(selected) - log.done
        for entry in select(iter_json_array(path), limit=limit, per_type=per_type, ids=ids, types=types):
            qid = entry["question_id"]
            if qid not in wanted:
                continue
            units = units_for(entry)
            ingest = ctx.system.open(qid, units)
            try:
                recall = ctx.system.recall(qid, entry["question"])
            finally:
                ctx.system.close(qid)
            prompt = template.format(render_history(recall.units), entry["question_date"], entry["question"])
            meta = {"question_id": qid, "question_type": entry["question_type"], "question": entry["question"],
                    "answer": entry["answer"]}
            yield qid, (meta, prompt, {**ingest, **recall.stats}, evidence(units, recall.units, entry))

    def work(qid: str, payload: Any) -> dict:
        meta, prompt, memory, ev = payload
        reply = ctx.reader.complete(prompt)
        hypothesis = reply.text
        check = official.anscheck_prompt(meta["question_type"], meta["question"], meta["answer"], hypothesis,
                                         abstention="_abs" in qid)
        verdict = ctx.judge.complete([{"role": "user", "content": check}], max_tokens=10)
        return {"id": qid, "question_id": qid, "question_type": meta["question_type"], "hypothesis": hypothesis,
                "autoeval_label": {"model": judge_model, "label": "yes" in verdict.text.strip().lower()},
                "judge_reply": verdict.text, "reader_model": reply.model, "judge_model": verdict.model,
                "reader_usage": reply.usage, "memory": memory, "evidence": ev}

    stop = execute(log, jobs(), work, workers=ctx.workers, meter=ctx.meter, max_usd=ctx.max_usd)
    rows = log.rows()
    summary = summarize(rows, ctx.meter, stop, expected)
    predictions = "".join(json.dumps({"question_id": r["question_id"], "hypothesis": r["hypothesis"]},
                                     ensure_ascii=False) + "\n" for r in rows if r.get("status") == "ok")
    return log.finish(summary, {"predictions.jsonl": predictions})


def summarize(rows: list[dict], meter=None, stop: str | None = None, expected: int | None = None) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    by_type: dict[str, list[int]] = defaultdict(list)
    abstention: list[int] = []
    for r in ok:
        score = 1 if r["autoeval_label"]["label"] else 0
        by_type[r["question_type"]].append(score)
        if "_abs" in r["question_id"]:
            abstention.append(score)
    type_accuracy = {t: {"accuracy": mean(v), "n": len(v)} for t, v in sorted(by_type.items())}
    summary = common_summary(rows, meter, stop, expected if expected is not None else len(rows))
    summary.update({
        "benchmark": "longmemeval",
        "overall_accuracy": mean([s for v in by_type.values() for s in v]),
        "task_averaged_accuracy": mean([sum(v) / len(v) for v in by_type.values()]),
        "abstention_accuracy": {"accuracy": mean(abstention), "n": len(abstention)},
        "by_question_type": type_accuracy,
        "evidence_session_recall": mean([r["evidence"]["session_recall"] for r in ok
                                         if "session_recall" in r.get("evidence", {}) and "_abs" not in r["question_id"]]),
        "evidence_round_recall": mean([r["evidence"]["round_recall"] for r in ok
                                       if "round_recall" in r.get("evidence", {}) and "_abs" not in r["question_id"]]),
        "judge_model": ok[0]["autoeval_label"]["model"] if ok else None,
    })
    return summary
