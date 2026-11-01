"""LongMemEval (Wu et al., ICLR 2025): 500 questions, each over its own chat history.

Reader prompt: the official template from run_generation.py (chain-of-thought by default), with the
history rendered in the official "nl" session format. Judge: the official get_anscheck_prompt from
evaluate_qa.py with gpt-4o-2024-08-06, temperature 0, max_tokens 10, label = "yes" in the reply.
judgments.jsonl rows carry `question_id` and `autoeval_label`, so LongMemEval's own
print_qa_metrics.py can score a run directly; predictions.jsonl holds the hypotheses in the
format evaluate_qa.py reads. Every row, paid or screen, carries the session and round recall of the
packed context and, per has_answer turn, how much of the turn the reader prompt holds inside the block of the
turn's own session (answer_coverage), with the session-blind value and where in the prompt the turn sits.
"""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .common import Context, base_config, common_summary, iter_json_array, mean, prompt_sha256, screen_row, screen_summary
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


def render_history_spans(units: list[Unit]) -> tuple[str, dict[Any, tuple[int, int]]]:
    """render_history's string and, per session it renders, the [start, end) characters of that session's block
    ('\\n### Session i: ... \\n'); the blocks follow one another, so the spans partition the string. A span is keyed
    by the session part of the units' order: LongMemEval units carry order (date, haystack session index, round,
    ...), so the key is the haystack session index answer turns are numbered by."""
    sessions: OrderedDict[tuple, tuple[str, list[dict]]] = OrderedDict()
    for unit in sorted(units, key=lambda u: u.order):
        sessions.setdefault(unit.order[:2], (unit.date, []))[1].extend(unit.turns)
    history = ""
    spans: dict[Any, tuple[int, int]] = {}
    for i, (key, (date, turns)) in enumerate(sessions.items(), 1):
        sess = "".join("\n\n{}: {}".format(t["role"], t["content"].strip()) for t in turns)
        block = f"\n### Session {i}:\nSession Date: {date}\nSession Content:\n{sess}\n"
        spans[key[-1]] = (len(history), len(history) + len(block))
        history += block
    return history, spans


def render_history(units: list[Unit]) -> str:
    """The official nl rendering: selected turns grouped into their sessions, sessions by date."""
    return render_history_spans(units)[0]


_FIELD = "\x00history\x00"


def history_offset(template: str, *rest: str) -> int:
    """Where the history, the template's first field, starts in template.format(history, *rest)."""
    return template.format(_FIELD, *rest).index(_FIELD)


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
    """Session and round recall of the chosen context. Under --pair-window / --window a chosen item is one window
    of a round (uid `<round>#p<k>` / `<round>#<j>`); it counts for the round it was cut from (meta parent_uid)."""
    answer_sessions = {u.session for u in units if u.meta.get("answer_session")}
    answer_turns = {u.uid for u in units if u.meta.get("has_answer")}
    chosen_sessions = {u.session for u in chosen}
    chosen_ids = {u.meta.get("parent_uid", u.uid) for u in chosen}
    out: dict[str, Any] = {"answer_sessions": len(answer_sessions), "answer_rounds": len(answer_turns)}
    if answer_sessions:
        out["session_recall"] = round(len(answer_sessions & chosen_sessions) / len(answer_sessions), 4)
    if answer_turns:
        out["round_recall"] = round(len(answer_turns & chosen_ids) / len(answer_turns), 4)
    return out


COVERAGE_BLOCK = 64  # characters; a shorter verbatim run of an answer turn counts only when it is the whole turn


def _ws(text: str) -> str:
    """`text` with each run of whitespace made one space, trimmed."""
    return " ".join(text.split())


def _windows(text: str) -> set[str]:
    """Every COVERAGE_BLOCK-character window of `text`."""
    return {text[i:i + COVERAGE_BLOCK] for i in range(len(text) - COVERAGE_BLOCK + 1)}


def _covered(body: str, text: str, windows: Callable[[], set[str]]) -> tuple[int, int | None]:
    """How many characters of `body` (normalized) `text` holds, whole or in blocks of at least COVERAGE_BLOCK
    characters in any order, and the offset in `text` of the first covered character (None when none is)."""
    n = len(body)
    if n and body in text:
        return n, text.index(body)
    if n <= COVERAGE_BLOCK:
        return 0, None
    found = windows()
    covered = end = 0
    first = None
    for a in range(n - COVERAGE_BLOCK + 1):  # the union of the turn's windows found in `text`
        if body[a:a + COVERAGE_BLOCK] in found:
            if first is None:
                first = a
            covered += a + COVERAGE_BLOCK - max(a, end)
            end = a + COVERAGE_BLOCK
    return covered, None if first is None else text.index(body[first:first + COVERAGE_BLOCK])


def _bp(covered: int, n: int) -> int:
    """10000 x covered / n: 10000 only when whole, 0 only when absent, so a partial turn reads 1..9999."""
    bp = round(10000 * covered / n) if n else 0
    return min(9999, max(1, bp)) if 0 < covered < n else bp


def _segment(prompt: str, text: str, start: int, end: int) -> tuple[str, int]:
    """prompt[start:end] whitespace-normalized and its offset in `text` (the normalized prompt)."""
    seg = _ws(prompt[start:end])
    head = _ws(prompt[:start])
    at = len(head) + (1 if head and seg else 0)
    if text[at:at + len(seg)] != seg:
        raise ValueError(f"the span {start}:{end} cuts a word of the prompt")
    return seg, at


def answer_coverage(entry: dict, prompt: str, spans: dict[int, tuple[int, int]] | None = None) -> dict[str, Any]:
    """How much of each has_answer turn the reader prompt holds, read from the prompt string itself (the string
    prompt_sha256 digests), not from what the packer reports. On whitespace-normalized text, a turn's characters
    are covered when they lie in a block of at least COVERAGE_BLOCK characters found verbatim, in any order, or
    the turn is there whole. coverage_bp is anchored: the blocks of answer turn (session si, turn ti) count only
    inside spans[si], the characters of the prompt that render the turn's own session; a session without a span
    is not in the prompt, so its answer turns read 0. unanchored_coverage_bp matches anywhere in the prompt (the
    phase-P10 instrument). A covered turn's position_bp is 10000 x the offset of its first covered character in
    the normalized prompt / the normalized prompt's length. Per question: answer_coverage_bp and
    unanchored_coverage_bp are the means over its answer turns, answer_position_bp the mean position over its
    covered turns, answer_share_bp the anchored covered characters per 10000 characters of the normalized
    prompt. spans=None anchors nothing (every session spans the whole prompt), for prompts built by hand."""
    text = _ws(prompt)
    segments = None if spans is None else {si: _segment(prompt, text, a, b) for si, (a, b) in spans.items()}
    memo: dict[Any, set[str]] = {}  # windows of the prompt (key None) and of each session's span, built when needed

    def windows_of(key: Any, s: str) -> Callable[[], set[str]]:
        def get() -> set[str]:
            if key not in memo:
                memo[key] = _windows(s)
            return memo[key]
        return get

    turns: list[dict[str, Any]] = []
    covered_all = 0
    for si, session in enumerate(entry["haystack_sessions"]):
        for ti, turn in enumerate(session):
            if not turn.get("has_answer"):
                continue
            body = _ws(str(turn.get("content", "")))
            n = len(body)
            loose, first = _covered(body, text, windows_of(None, text))
            if segments is None:
                covered = loose
            elif si in segments:
                seg, at = segments[si]
                covered, first = _covered(body, seg, windows_of(si, seg))
                first = None if first is None else at + first
            else:
                covered, first = 0, None
            row: dict[str, Any] = {"session": si, "turn": ti, "role": str(turn.get("role", "user")), "chars": n,
                                   "coverage_bp": _bp(covered, n), "unanchored_coverage_bp": _bp(loose, n)}
            if covered and first is not None:
                row["position_bp"] = round(10000 * first / len(text))
            turns.append(row)
            covered_all += covered
    out: dict[str, Any] = {"answer_turns": turns,
                           "answer_share_bp": round(10000 * covered_all / len(text)) if text else 0}
    if turns:
        out["answer_coverage_bp"] = round(sum(t["coverage_bp"] for t in turns) / len(turns))
        out["unanchored_coverage_bp"] = round(sum(t["unanchored_coverage_bp"] for t in turns) / len(turns))
        positions = [t["position_bp"] for t in turns if "position_bp" in t]
        if positions:
            out["answer_position_bp"] = round(sum(positions) / len(positions))
    return out


def coverage_summary(rows: list[dict]) -> dict[str, Any]:
    """answer_coverage_bp (anchored) and unanchored_coverage_bp averaged over the non-abstention questions, the
    averaging round_recall uses; their answer turns counted whole (10000), partial and absent (0), anchored;
    answer_share_bp beside them, not gated."""
    ev = [r.get("evidence", {}) for r in rows
          if r.get("status") == "ok" and "_abs" not in str(r.get("question_id", r.get("id", "")))]
    turns = [t["coverage_bp"] for e in ev for t in e.get("answer_turns", [])]
    coverage = [e["answer_coverage_bp"] for e in ev if "answer_coverage_bp" in e]
    loose = [e["unanchored_coverage_bp"] for e in ev if "unanchored_coverage_bp" in e]
    share = [e["answer_share_bp"] for e in ev if "answer_share_bp" in e]
    return {"answer_coverage_bp": round(sum(coverage) / len(coverage)) if coverage else None,
            "unanchored_coverage_bp": round(sum(loose) / len(loose)) if loose else None,
            "answer_turns_whole": sum(1 for bp in turns if bp == 10000),
            "answer_turns_partial": sum(1 for bp in turns if 0 < bp < 10000),
            "answer_turns_absent": sum(1 for bp in turns if bp == 0),
            "answer_share_bp": round(sum(share) / len(share)) if share else None}


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
                recall = ctx.system.recall(qid, entry["question"], render_history)
            finally:
                ctx.system.close(qid)
            history, spans = render_history_spans(recall.units)
            prompt = template.format(history, entry["question_date"], entry["question"])
            at = history_offset(template, entry["question_date"], entry["question"])
            if prompt[at:at + len(history)] != history:
                raise RuntimeError(f"{qid}: the history is not at offset {at} of the reader prompt")
            meta = {"question_id": qid, "question_type": entry["question_type"], "question": entry["question"],
                    "answer": entry["answer"]}
            anchored = {si: (at + a, at + b) for si, (a, b) in spans.items()}
            ev = {**evidence(units, recall.units, entry), **answer_coverage(entry, prompt, anchored)}
            yield qid, (meta, prompt, {**ingest, **recall.stats}, ev)

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
                "reader_usage": reply.usage, "memory": memory, "evidence": ev,
                "prompt_sha256": prompt_sha256(prompt)}  # the prompt the reader received, as a screen row digests it

    if ctx.retrieval_only:  # a $0 screen: memory and evidence recall only, no reader or judge calls
        def screen(job_id: str, payload: Any) -> dict:
            meta, prompt, memory, ev = payload
            return screen_row(job_id, meta["question_type"], memory, ev, prompt)

        stop = execute(log, jobs(), screen, workers=ctx.workers, meter=ctx.meter, max_usd=None)
        rows = log.rows()
        return log.finish({**screen_summary(rows, stop, expected, "longmemeval", ("session_recall", "round_recall")),
                           **coverage_summary(rows)})

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
        **coverage_summary(rows),
    })
    return summary
