"""BEAM (Tavakoli et al., 2025): 100 conversations from 100K to 10M tokens, 20 probing questions each.

Ten memory abilities, two questions per ability per conversation. The reader prompt is BEAM's
answer_generation_for_rag; each answer is scored by BEAM's own evaluator for its ability, with
gpt-4.1-mini at temperature 0 as the judge, as in BEAM's src/llm.py. A question's score is tau_norm
for event ordering and llm_judge_score for the other abilities (report_results.py); an ability's
score is the mean over conversations of each conversation's mean, and the overall score is the
mean of the ability scores.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .common import Context, base_config, common_summary, mean, screen_row, screen_summary
from .memory import Unit
from .official import BEAM_ABILITIES, BeamOfficial
from .runlog import RunLog, execute
from .sources import beam_conversations, fetch, record

OFFICIAL_JUDGE = "gpt-4.1-mini"


def _batches(chat: list) -> list[dict]:
    batches: list[dict] = []
    for element in chat:
        if isinstance(element, dict) and "turns" in element:
            batches.append(element)
        elif isinstance(element, dict):  # the 10M conversations nest their batches under plan names
            for plan, items in element.items():
                for batch in items or []:
                    if isinstance(batch, dict):
                        batches.append({**batch, "plan": plan})
    return batches


def units_for(chat: list) -> list[Unit]:
    units: list[Unit] = []
    for bi, batch in enumerate(_batches(chat)):
        for ti, group in enumerate(batch.get("turns") or []):
            messages = [m for m in (group if isinstance(group, list) else [group])
                        if isinstance(m, dict) and str(m.get("content", "")).strip()]
            if not messages:
                continue
            date = next((str(m["time_anchor"]) for m in messages if m.get("time_anchor")), "")
            text = "\n".join(f"{m.get('role', 'user')}: {str(m['content']).strip()}" for m in messages)
            session = f"{batch.get('plan', '')}/batch-{batch.get('batch_number', bi)}"
            units.append(Unit(uid=f"{bi}:{ti}", session=session, order=(bi, ti), date=date, text=text,
                              rendered=(f"[{date}]\n" if date else "") + text,
                              turns=[{"role": m.get("role", "user"), "content": str(m["content"])} for m in messages]))
    return units


def render(units: list[Unit]) -> str:
    return "\n\n".join(u.rendered for u in sorted(units, key=lambda u: u.order))


def load_questions(size: str, conversation: str, data_dir: Path | None) -> dict:
    if data_dir:
        path = Path(data_dir) / conversation / "probing_questions" / "probing_questions.json"
    else:
        path = fetch(f"beam/chats/{size}/{conversation}/probing_questions/probing_questions.json")
    return json.loads(path.read_text("utf-8"))


def load_chat(size: str, conversation: str, data_dir: Path | None) -> list:
    path = Path(data_dir) / conversation / "chat.json" if data_dir else fetch(f"beam/chats/{size}/{conversation}/chat.json")
    return json.loads(path.read_text("utf-8"))


def _clean(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def run(ctx: Context, *, size: str = "100K", conversations: list[str] | None = None, per_ability: int | None = None,
        abilities: list[str] | None = None, data_dir: Path | None = None, official: BeamOfficial | None = None,
        reader_max_tokens: int = 2048) -> Path:
    official = official or BeamOfficial()
    abilities = abilities or list(BEAM_ABILITIES)
    conv_ids = conversations or (sorted(p.name for p in Path(data_dir).iterdir() if p.is_dir()) if data_dir
                                 else beam_conversations(size))
    config = base_config(ctx, {"benchmark": "beam", "size": size, "conversations": conv_ids,
                               "per_ability": per_ability, "abilities": abilities, "reader_max_tokens": reader_max_tokens})
    sources = [record("beam/compute_metrics.py"), record("beam/prompts.py")]
    if not data_dir:
        for conv in conv_ids:
            sources.append(record(f"beam/chats/{size}/{conv}/chat.json"))
            sources.append(record(f"beam/chats/{size}/{conv}/probing_questions/probing_questions.json"))
    log = RunLog(ctx.runs_root, "beam", config, sources, ctx.resume)
    expected = 0
    plan: list[tuple[str, list[tuple[str, int, dict]]]] = []
    for conv in conv_ids:
        probing = load_questions(size, conv, data_dir)
        items = [(a, i, q) for a in abilities for i, q in enumerate((probing.get(a) or [])[:per_ability])]
        expected += len(items)
        plan.append((conv, items))

    def jobs() -> Iterator[tuple[str, Any]]:
        for conv, items in plan:
            todo = [(a, i, q) for a, i, q in items if f"{size}/{conv}/{a}/{i}" not in log.done]
            if not todo:
                continue
            chat = load_chat(size, conv, data_dir)
            units = units_for(chat)
            del chat
            history_id = f"{size}/{conv}"
            ingest = ctx.system.open(history_id, units)
            try:
                for ability, i, q in todo:
                    recall = ctx.system.recall(history_id, str(q["question"]))
                    prompt = official.answer(render(recall.units), str(q["question"]))
                    yield f"{size}/{conv}/{ability}/{i}", (conv, ability, i, q, prompt, {**ingest, **recall.stats})
            finally:
                ctx.system.close(history_id)

    def work(job_id: str, payload: Any) -> dict:
        conv, ability, i, q, prompt, memory = payload
        reply = ctx.reader.complete(prompt, max_tokens=reader_max_tokens)
        result = _clean(official.evaluate(ability, q["rubric"], reply.text, str(q["question"]), ctx.judge))
        try:
            score = official.score(ability, result)
        except (TypeError, ValueError, KeyError):
            score = None
        return {"id": job_id, "size": size, "conversation": conv, "ability": ability, "index": i,
                "question": q["question"], "answer": reply.text, "score": score, "score_was_nan": score is None,
                "evaluation": result, "reader_model": reply.model, "reader_usage": reply.usage, "memory": memory}

    if ctx.retrieval_only:  # a $0 screen: memory and evidence recall only, no reader or judge calls
        def screen(job_id: str, payload: Any) -> dict:
            conv, ability, i, q, prompt, memory = payload
            return screen_row(job_id, ability, memory, None, prompt)

        stop = execute(log, jobs(), screen, workers=ctx.workers, meter=ctx.meter, max_usd=None)
        return log.finish(screen_summary(log.rows(), stop, expected, "beam", ()))

    stop = execute(log, jobs(), work, workers=ctx.workers, meter=ctx.meter, max_usd=ctx.max_usd)
    return log.finish(summarize(log.rows(), ctx.meter, stop, expected))


def summarize(rows: list[dict], meter=None, stop: str | None = None, expected: int | None = None) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    per_conv: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    nan_scores = 0
    for r in ok:
        if r["score"] is None:
            nan_scores += 1  # an undefined Kendall tau counts as 0, the lowest score
        per_conv[r["ability"]][r["conversation"]].append(r["score"] or 0.0)
    ability_scores = {a: {"score": mean([sum(v) / len(v) for v in convs.values()]),
                          "conversations": len(convs), "questions": sum(len(v) for v in convs.values())}
                      for a, convs in sorted(per_conv.items())}
    summary = common_summary(rows, meter, stop, expected if expected is not None else len(rows))
    summary.update({
        "benchmark": "beam",
        "overall": mean([s["score"] for s in ability_scores.values() if s["score"] is not None]),
        "by_ability": ability_scores,
        "undefined_scores_counted_as_zero": nan_scores,
    })
    return summary
