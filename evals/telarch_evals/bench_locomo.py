"""LoCoMo (Maharana et al., ACL 2024): 10 long conversations between two people, 1,986 questions.

Scored the way most published LoCoMo comparisons are: an LLM judge ("J") with the ACCURACY_PROMPT
from mem0's evaluation code, gpt-4o-mini, JSON output, temperature 0, over categories 1 to 4
(category 5, adversarial, is left out unless asked for). Token F1 is reported alongside it. The
reader prompt below is this harness's own and is part of the system under test.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .common import Context, base_config, common_summary, mean
from .llm import LLMError
from .memory import Unit
from .official import LoCoMoJudge
from .runlog import RunLog, execute
from .sources import fetch, record
from .textutil import token_f1

# Category names follow the mapping used by most published LoCoMo comparisons.
CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}

READER_PROMPT = """You are answering a question about a long conversation between {speaker_a} and {speaker_b}. Below are the parts of that conversation recalled from memory, each with the date and time it was said.

{context}

Question: {question}

Answer with a short phrase, usually fewer than ten words, using only the conversation above. Turn relative times such as "last week" or "yesterday" into the date they refer to, counting from when that line was said. If the conversation does not settle the question, give the answer it supports best.

Answer:"""

_DIA = re.compile(r"D\d+:\d+")


def units_for(sample: dict) -> list[Unit]:
    conv = sample["conversation"]
    numbers = sorted(int(m.group(1)) for k in conv if (m := re.fullmatch(r"session_(\d+)", k)))
    units: list[Unit] = []
    seen: dict[str, int] = {}
    for n in numbers:
        date = str(conv.get(f"session_{n}_date_time", ""))
        for ti, turn in enumerate(conv.get(f"session_{n}") or []):
            text = str(turn.get("text", "")).strip()
            if turn.get("blip_caption"):
                text += f" [shared an image: {turn['blip_caption']}]"
            speaker = str(turn.get("speaker", "speaker"))
            uid = str(turn.get("dia_id") or f"D{n}:{ti + 1}")
            seen[uid] = seen.get(uid, 0) + 1
            if seen[uid] > 1:
                uid = f"{uid}#{seen[uid]}"
            units.append(Unit(uid=uid, session=f"session_{n}", order=(n, ti), date=date, text=f"{speaker}: {text}",
                              rendered=f"[{date}] {speaker}: {text}", turns=[{"speaker": speaker, "content": text}]))
    return units


def render(units: list[Unit]) -> str:
    lines: list[str] = []
    last_session = None
    for unit in sorted(units, key=lambda u: u.order):
        if last_session is not None and unit.session != last_session:
            lines.append("")
        lines.append(unit.rendered)
        last_session = unit.session
    return "\n".join(lines) if lines else "(nothing recalled)"


def questions(sample: dict, categories: set[int], per_conversation: int | None) -> list[tuple[int, dict]]:
    out = [(i, qa) for i, qa in enumerate(sample.get("qa", [])) if int(qa.get("category", 0)) in categories]
    return out[:per_conversation] if per_conversation is not None else out


def gold_answer(qa: dict) -> str:
    if int(qa.get("category", 0)) == 5:
        return str(qa.get("adversarial_answer", qa.get("answer", "")))
    return str(qa.get("answer", ""))


def run(ctx: Context, *, conversations: int | None = None, per_conversation: int | None = None,
        categories: set[int] | None = None, data_path: Path | None = None, judge_prompt: LoCoMoJudge | None = None) -> Path:
    judge_prompt = judge_prompt or LoCoMoJudge()
    categories = categories or {1, 2, 3, 4}
    path = Path(data_path) if data_path else fetch("locomo/locomo10.json")
    data = json.loads(path.read_text("utf-8"))
    samples = data[:conversations] if conversations else data
    config = base_config(ctx, {"benchmark": "locomo", "conversations": [s["sample_id"] for s in samples],
                               "per_conversation": per_conversation, "categories": sorted(categories),
                               "reader_prompt": READER_PROMPT})
    sources = ([] if data_path else [record("locomo/locomo10.json")]) + [record("mem0/llm_judge.py")]
    log = RunLog(ctx.runs_root, "locomo", config, sources, ctx.resume)
    expected = sum(len(questions(s, categories, per_conversation)) for s in samples)

    def jobs() -> Iterator[tuple[str, Any]]:
        for sample in samples:
            sid = str(sample["sample_id"])
            todo = [(i, qa) for i, qa in questions(sample, categories, per_conversation) if f"{sid}/{i}" not in log.done]
            if not todo:
                continue
            units = units_for(sample)
            ingest = ctx.system.open(sid, units)
            try:
                for i, qa in todo:
                    recall = ctx.system.recall(sid, str(qa["question"]))
                    chosen = {u.uid.split("#")[0] for u in recall.units}
                    gold_ids = set(_DIA.findall(" ".join(map(str, qa.get("evidence") or []))))
                    ev: dict[str, Any] = {"evidence": len(gold_ids)}
                    if gold_ids:
                        ev["evidence_recall"] = round(len(gold_ids & chosen) / len(gold_ids), 4)
                    prompt = READER_PROMPT.format(speaker_a=sample["conversation"].get("speaker_a", "A"),
                                                  speaker_b=sample["conversation"].get("speaker_b", "B"),
                                                  context=render(recall.units), question=qa["question"])
                    yield f"{sid}/{i}", (sid, qa, prompt, {**ingest, **recall.stats}, ev)
            finally:
                ctx.system.close(sid)

    def work(job_id: str, payload: Any) -> dict:
        sid, qa, prompt, memory, ev = payload
        reply = ctx.reader.complete(prompt)
        answer = reply.text.strip()
        gold = gold_answer(qa)
        verdict = ctx.judge.complete(judge_prompt.render(str(qa["question"]), gold, answer),
                                     response_format={"type": "json_object"})
        label = judge_prompt.label(verdict.text)
        if label is None:
            raise LLMError("judge reply has no label")
        category = int(qa.get("category", 0))
        return {"id": job_id, "conversation": sid, "category": category,
                "category_name": CATEGORY_NAMES.get(category, str(category)), "question": qa["question"],
                "gold": gold, "answer": answer, "j": 1 if label == "CORRECT" else 0, "judge_label": label,
                "f1": round(token_f1(answer, gold), 4), "reader_model": reply.model, "judge_model": verdict.model,
                "reader_usage": reply.usage, "memory": memory, "evidence": ev}

    stop = execute(log, jobs(), work, workers=ctx.workers, meter=ctx.meter, max_usd=ctx.max_usd)
    return log.finish(summarize(log.rows(), ctx.meter, stop, expected))


def summarize(rows: list[dict], meter=None, stop: str | None = None, expected: int | None = None) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    j_by: dict[str, list[int]] = defaultdict(list)
    f1_by: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        j_by[r["category_name"]].append(r["j"])
        f1_by[r["category_name"]].append(r["f1"])
    summary = common_summary(rows, meter, stop, expected if expected is not None else len(rows))
    summary.update({
        "benchmark": "locomo",
        "j_overall": mean([r["j"] for r in ok]),
        "f1_overall": mean([r["f1"] for r in ok]),
        "by_category": {name: {"j": mean(j_by[name]), "f1": mean(f1_by[name]), "n": len(j_by[name])}
                        for name in sorted(j_by)},
        "evidence_recall": mean([r["evidence"]["evidence_recall"] for r in ok if "evidence_recall" in r.get("evidence", {})]),
    })
    return summary
