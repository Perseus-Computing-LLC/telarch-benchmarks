"""Abstention audit: does the judge's credit on an abstention question match whether the answer declined?

An abstention question asks for something the history does not contain; the right answer says so. A judge can
give full marks to an answer that supplies the missing detail (measured on another system's published BEAM 100K
records: 26 of 32 perfect abstention scores). This reads a finished run's rows and cross-tabulates the judge's
credit with a text check of the answer:

  telarch-evals abstention-audit RUN_DIR [--show suspects|all|none]

LongMemEval: the questions whose id ends in `_abs`. BEAM: the `abstention` ability. The text check is a list of
declining phrases, so it sorts the rows for reading and does not replace it: every row in a disagreeing cell
(credited without a declining phrase, or declining without credit) is printed for a hand read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .noise_floor import answer, load, score

# An answer that says the history lacks what was asked.
DECLINE = re.compile(
    r"\b(?:"
    r"(?:no|not any|without any|lack(?:s|ing)? (?:of )?(?:any )?) ?(?:specific |explicit |direct |relevant |further |prior |such )*"
    r"(?:information|mention|record|details?|indication|reference|evidence|data|context|discussion)"
    r"|(?:not|never|n[o']t) (?:been )?(?:explicitly |specifically |directly |actually |previously )?"
    r"(?:mention(?:ed)?|specif(?:y|ied)|provide[ds]?|state[ds]?|discuss(?:ed)?|share[ds]?|indicate[ds]?|cover(?:ed)?"
    r"|record(?:ed)?|include[ds]?|available|known|clear|given|said|told|address(?:ed)?)"
    r"|(?:do(?:es)?n[o']t|do(?:es)? not|did not|didn[o']t|cannot|can[o']t|could not|couldn[o']t|unable to) "
    r"(?:have|know|find|determine|tell|say|see|recall|answer|provide|confirm|identify|access)"
    r"|i(?: a|')m not (?:sure|aware|able)"
    r"|unknown|unclear|not enough|insufficient"
    r")\b", re.IGNORECASE)


def declines(text: str) -> bool:
    return bool(DECLINE.search(text))


def audit(run_dir: Path, questions: dict[str, dict] | None = None) -> dict[str, Any]:
    """`questions` maps a row id to {"question", "gold"} for rows that do not carry them (LongMemEval)."""
    questions = questions or {}
    manifest, rows = load(Path(run_dir))
    benchmark = manifest["benchmark"]
    if benchmark == "longmemeval":
        picked = [r for qid, r in sorted(rows.items()) if qid.endswith("_abs")]
    elif benchmark == "beam":
        picked = [r for _, r in sorted(rows.items()) if r.get("ability") == "abstention"]
    else:
        raise SystemExit(f"abstention-audit reads longmemeval and beam runs, not {benchmark}")
    cells = {"credited_declining": 0, "credited_not_declining": 0, "partial_declining": 0,
             "partial_not_declining": 0, "uncredited_declining": 0, "uncredited_not_declining": 0}
    out_rows = []
    for row in picked:
        s = score(benchmark, row)
        text = answer(benchmark, row)
        said_no = declines(text)
        credit = "credited" if s >= 1.0 else "uncredited" if s <= 0.0 else "partial"
        cells[f"{credit}_{'declining' if said_no else 'not_declining'}"] += 1
        judge = row.get("judge_reply") if benchmark == "longmemeval" else "; ".join(
            str(j.get("reason", "")) for j in (row.get("evaluation", {}).get("llm_judge_responses") or [])
            if isinstance(j, dict))
        out_rows.append({"id": row["id"], "score": s, "declines_by_phrase": said_no,
                         "suspect": (credit != "uncredited") != said_no,
                         "question": row.get("question") or questions.get(row["id"], {}).get("question"),
                         "gold": questions.get(row["id"], {}).get("gold"), "answer": text, "judge": judge})
    n = len(out_rows)
    return {
        "run_id": manifest["run_id"], "benchmark": benchmark, "abstention_questions": n,
        "mean_score_bp": round(sum(r["score"] for r in out_rows) / n * 10000) if n else None,
        "declining_by_phrase": sum(r["declines_by_phrase"] for r in out_rows),
        "cells": cells, "suspects": sum(r["suspect"] for r in out_rows), "rows": out_rows,
    }


def render(result: dict[str, Any], show: str = "suspects", width: int = 1200) -> str:
    lines = [f"{result['benchmark']} {result['run_id']}: {result['abstention_questions']} abstention questions, "
             f"mean score {result['mean_score_bp']} bp, {result['declining_by_phrase']} answers with a declining phrase",
             "  " + ", ".join(f"{k} {v}" for k, v in result["cells"].items()),
             f"  {result['suspects']} rows where credit and the phrase check disagree (read these by hand)"]
    for row in result["rows"]:
        if show == "all" or (show == "suspects" and row["suspect"]):
            lines += ["", f"--- {row['id']}  score {row['score']}  declining phrase: {row['declines_by_phrase']}",
                      f"Q: {row['question'] or '(not in the row)'}"] + (
                          [f"GOLD: {row['gold']}"] if row.get("gold") else []) + [
                      f"A: {row['answer'][-width:]}", f"JUDGE: {str(row['judge'])[:width]}"]
    return "\n".join(lines)
