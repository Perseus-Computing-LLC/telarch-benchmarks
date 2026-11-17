"""Noise floor of a paid comparison: what re-answering and re-judging the control's own contexts changes.

A replay is the control run again with `--no-cache`: the same code and settings, so memory hands the reader the
same context for every question, and every reader and judge call is a fresh draw. Whatever differs between the
control and its replay is the models, not the memory system. A candidate's gain is read against that:

  telarch-evals noise-floor CONTROL --replay REPLAY [--replay ...] [--candidate RUN ...] [--out FILE]

The record refuses to call a replay a replay unless each paired question's reader prompt is the control's
(`prompt_sha256` in the rows) and the replay made fresh draws (`no_cache` in its manifest). For each candidate it
gives the questions that went up and down, split by whether the candidate changed the prompt: a flip on an
unchanged prompt is model noise by construction.

Scores are per question: LongMemEval's label, LoCoMo's J, BEAM's rubric score (0 to 1, undefined counted as 0).
The BEAM mean here is the mean question score, not the benchmark's ability-macro average.
"""

from __future__ import annotations

import json
from math import comb
from pathlib import Path
from typing import Any


def load(run_dir: Path) -> tuple[dict, dict[str, dict]]:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    rows: dict[str, dict] = {}
    with open(run_dir / "judgments.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                if row.get("status") == "ok":
                    rows[row["id"]] = row
    return manifest, rows


def score(benchmark: str, row: dict) -> float:
    if benchmark == "longmemeval":
        return 1.0 if row["autoeval_label"]["label"] else 0.0
    if benchmark == "locomo":
        return float(row["j"])
    if benchmark == "beam":
        return float(row["score"] or 0.0)
    raise SystemExit(f"noise-floor reads longmemeval, locomo and beam runs, not {benchmark}")


def group(benchmark: str, row: dict) -> str:
    return str(row.get({"longmemeval": "question_type", "locomo": "category_name", "beam": "ability"}[benchmark], ""))


def answer(benchmark: str, row: dict) -> str:
    return str(row.get("hypothesis" if benchmark == "longmemeval" else "answer", ""))


def _bp(x: float) -> int:
    return round(x * 10000)


def sign_test_p(up: int, down: int) -> float:
    """Two-sided exact binomial test of up against down at p = 1/2 (1.0 when nothing changed)."""
    n, k = up + down, min(up, down)
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def pair(benchmark: str, base: dict[str, dict], other: dict[str, dict]) -> dict[str, Any]:
    """`other` against `base` on the questions both answered."""
    ids = sorted(set(base).intersection(other))
    up = down = same_prompt = changed_prompt = no_digest = answers_changed = 0
    up_same = down_same = up_changed = down_changed = 0
    by_group: dict[str, dict[str, int]] = {}
    total_base = total_other = 0.0
    for qid in ids:
        a, b = base[qid], other[qid]
        sa, sb = score(benchmark, a), score(benchmark, b)
        total_base += sa
        total_other += sb
        da, db = a.get("prompt_sha256"), b.get("prompt_sha256")
        if da is None or db is None:
            no_digest += 1
            prompt_same = None
        else:
            prompt_same = da == db
            same_prompt += prompt_same
            changed_prompt += not prompt_same
        answers_changed += answer(benchmark, a) != answer(benchmark, b)
        g = by_group.setdefault(group(benchmark, a), {"n": 0, "up": 0, "down": 0})
        g["n"] += 1
        if sb > sa:
            up += 1
            g["up"] += 1
            up_same += prompt_same is True
            up_changed += prompt_same is False
        elif sb < sa:
            down += 1
            g["down"] += 1
            down_same += prompt_same is True
            down_changed += prompt_same is False
    n = len(ids)
    return {
        "n": n, "only_in_base": len(set(base) - set(other)), "only_in_other": len(set(other) - set(base)),
        "up": up, "down": down, "changed": up + down, "net": up - down,
        "changed_bp": _bp((up + down) / n) if n else None,
        "mean_base_bp": _bp(total_base / n) if n else None, "mean_other_bp": _bp(total_other / n) if n else None,
        "delta_bp": _bp((total_other - total_base) / n) if n else None,
        "answers_changed": answers_changed,
        "prompts": {"same": same_prompt, "changed": changed_prompt, "no_digest": no_digest},
        "on_same_prompt": {"up": up_same, "down": down_same},
        "on_changed_prompt": {"up": up_changed, "down": down_changed},
        "sign_test_p": round(sign_test_p(up, down), 6),
        "by_group": dict(sorted(by_group.items())),
    }


def _same_question_set(a: dict, b: dict) -> bool:
    """Two manifests ask the same questions of the same data: everything but the memory system and no_cache."""
    skip = {"memory", "system", "no_cache"}
    ca, cb = a.get("config", {}), b.get("config", {})
    return {k: v for k, v in ca.items() if k not in skip} == {k: v for k, v in cb.items() if k not in skip}


def record(control: Path, replays: list[Path], candidates: list[Path] | None = None) -> dict[str, Any]:
    c_manifest, c_rows = load(control)
    benchmark = c_manifest["benchmark"]
    out: dict[str, Any] = {
        "benchmark": benchmark, "control": {"run_id": c_manifest["run_id"], "rows": len(c_rows),
                                            "repo_commit": c_manifest.get("repo_commit")},
        "replays": [], "candidates": [], "problems": [],
    }
    for path in replays:
        manifest, rows = load(path)
        entry = {"run_id": manifest["run_id"], **pair(benchmark, c_rows, rows),
                 "fresh_draws": bool(manifest.get("config", {}).get("no_cache")),
                 "resampled": "reader and judge" if manifest.get("config", {}).get("no_cache") else "nothing (cached)"}
        if manifest["benchmark"] != benchmark:
            out["problems"].append(f"{manifest['run_id']}: a {manifest['benchmark']} run")
        if {k: v for k, v in manifest.get("config", {}).items() if k != "no_cache"} != {
                k: v for k, v in c_manifest.get("config", {}).items() if k != "no_cache"}:
            out["problems"].append(f"{manifest['run_id']}: settings differ from the control's")
        if not entry["fresh_draws"]:
            out["problems"].append(f"{manifest['run_id']}: ran with the response cache, so it re-sampled nothing")
        if entry["prompts"]["changed"]:
            out["problems"].append(f"{manifest['run_id']}: {entry['prompts']['changed']} reader prompts differ from "
                                   "the control's; this is not a replay of its contexts")
        if entry["prompts"]["no_digest"]:
            out["problems"].append(f"{manifest['run_id']}: {entry['prompts']['no_digest']} paired rows carry no "
                                   "prompt_sha256, so identical contexts are not shown")
        if entry["only_in_base"] or entry["only_in_other"]:
            out["problems"].append(f"{manifest['run_id']}: question sets differ")
        out["replays"].append(entry)
    out["valid"] = bool(replays) and not out["problems"]
    floor_changed = max((r["changed"] for r in out["replays"]), default=None)
    floor_net = max((abs(r["net"]) for r in out["replays"]), default=None)
    floor_delta = max((abs(r["delta_bp"]) for r in out["replays"] if r["delta_bp"] is not None), default=None)
    out["floor"] = {"changed": floor_changed, "abs_net": floor_net, "abs_delta_bp": floor_delta,
                    "replays": len(out["replays"])}
    for path in candidates or []:
        manifest, rows = load(path)
        entry = {"run_id": manifest["run_id"], **pair(benchmark, c_rows, rows),
                 "same_question_set": _same_question_set(c_manifest, manifest)}
        if out["valid"]:
            entry["within_floor"] = abs(entry["delta_bp"]) <= floor_delta
            entry["reading"] = (
                f"{entry['up']} up, {entry['down']} down (net {entry['net']:+d}, {entry['delta_bp']:+d} bp) against "
                f"a replay of identical contexts that changed {floor_changed} questions and moved the mean by up to "
                f"{floor_delta} bp: " + ("inside the noise floor; not evidence of a difference"
                                         if entry["within_floor"] else "larger than the replay's movement"))
        out["candidates"].append(entry)
    return out
