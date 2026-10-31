"""Shared pieces of a benchmark run: the context object and the summary helpers."""

from __future__ import annotations

from collections import defaultdict

import hashlib
import json
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm import ChatClient, Meter
from .memory import MemorySystem
from .textutil import percentile


@dataclass
class Context:
    runs_root: Path
    workdir: Path
    system: MemorySystem
    reader: ChatClient
    judge: ChatClient
    meter: Meter
    workers: int = 4
    max_usd: float | None = None
    resume: Path | None = None
    retrieval_only: bool = False  # a $0 screen: memory only, no reader or judge calls
    no_cache: bool = False  # --no-cache: every model call goes to the provider (fresh draws); stored in the manifest


def base_config(ctx: Context, extra: dict[str, Any]) -> dict[str, Any]:
    """Settings that define a run. A resumed run must match them exactly."""
    config = {"system": ctx.system.name, "memory": ctx.system.config(), "reader": ctx.reader.describe(),
              "judge": ctx.judge.describe()}
    if ctx.retrieval_only:
        config["retrieval_only"] = True
    if ctx.no_cache:  # absent otherwise, so earlier runs' configs and their resumes are unchanged
        config["no_cache"] = True
    config.update(extra)
    return config


def iter_json_array(path: Path, chunk_size: int = 1 << 22) -> Iterator[Any]:
    """Yield the elements of a top-level JSON array without loading the whole file."""
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8") as fh:
        buf = fh.read(chunk_size)
        pos = 0
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf):
                break
            more = fh.read(chunk_size)
            if not more:
                return
            buf, pos = buf[pos:] + more, 0
        if buf[pos] != "[":
            raise ValueError(f"{path} is not a JSON array")
        pos += 1
        while True:
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n,":
                    pos += 1
                if pos < len(buf):
                    break
                more = fh.read(chunk_size)
                if not more:
                    raise ValueError(f"{path}: unterminated JSON array")
                buf, pos = more, 0
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    more = fh.read(chunk_size)
                    if not more:
                        raise
                    buf, pos = buf[pos:] + more, 0
            yield obj
            buf, pos = buf[end:], 0


def mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    values = list(values)
    return {"n": len(values), "mean": round(statistics.fmean(values), 2),
            "p50": round(percentile(values, 0.5) or 0, 2), "p95": round(percentile(values, 0.95) or 0, 2),
            "max": round(max(values), 2)}


def common_summary(rows: list[dict], meter: Meter | None, stop: str | None, expected: int) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok"]
    errors = [r for r in rows if r.get("status") != "ok"]
    retrieval = [r.get("memory", {}) for r in ok]
    summary: dict[str, Any] = {
        "rows": len(ok), "errors": len(errors), "expected": expected,
        "complete": len(ok) == expected and not errors and stop is None,
        "stopped": stop,
        "context_tokens": distribution([m["context_tokens"] for m in retrieval if "context_tokens" in m]),
        "joined_tokens_o200k": distribution([m["joined_tokens_o200k"] for m in retrieval if "joined_tokens_o200k" in m]),
        "units_selected": distribution([m["selected"] for m in retrieval if "selected" in m]),
        "search_ms": distribution([m["search_ms"] for m in retrieval if "search_ms" in m]),
        "harpe_ms": distribution([m["harpe_ms"] for m in retrieval if "harpe_ms" in m]),
        "reader_prompt_tokens": distribution([r["reader_usage"]["prompt_tokens"] for r in ok
                                              if r.get("reader_usage", {}).get("prompt_tokens") is not None]),
    }
    if errors:
        summary["first_errors"] = [e.get("error") for e in errors[:3]]
    if meter is not None:
        summary["usage"] = meter.snapshot()
    return summary


class OfflineClient:
    """Stands in for the reader and the judge in a retrieval-only screen, which makes no model calls."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.model = None

    def describe(self) -> dict[str, Any]:
        return {"model": None, "role": self.role, "retrieval_only": True}

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"retrieval-only run: no {self.role} call")

    invoke = complete

    def derive(self, *args: Any, **kwargs: Any) -> "OfflineClient":
        """Another offline stand-in: MemoryAgentBench derives its summary judge from the judge."""
        return OfflineClient(self.role)


_PROMPT_TOKENS: list[Any] = []


def prompt_sha256(prompt: Any) -> str:
    """Digest of the exact reader prompt: a string as it is, a message list as sorted-key JSON. A screen row and a
    paid row of one question carry the same digest, so a paid run can be matched against a $0 screen."""
    exact = prompt if isinstance(prompt, str) else json.dumps(prompt, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()


def screen_row(job_id: str, group: Any, memory: dict[str, Any], evidence: dict[str, Any] | None,
               prompt: Any) -> dict[str, Any]:
    """One question of a retrieval-only screen: what memory selected, its evidence recall, and the size of
    the reader prompt it would have produced, in reader tokens. `prompt_sha256` identifies that prompt, so two
    screens can show which questions' prompts a flag changed."""
    if not _PROMPT_TOKENS:
        from .memory import _o200k_counter

        _PROMPT_TOKENS.append(_o200k_counter())
    text = prompt if isinstance(prompt, str) else " ".join(str(m.get("content", "")) for m in prompt)
    return {"id": job_id, "group": str(group), "memory": memory, "evidence": evidence or {},
            "prompt_tokens_o200k": _PROMPT_TOKENS[0](text), "prompt_sha256": prompt_sha256(prompt)}


def screen_summary(rows: list[dict], stop: str | None, expected: int, benchmark: str,
                   recall_keys: Sequence[str]) -> dict[str, Any]:
    """Summary of a retrieval-only screen: evidence recall overall and per question group, and what was packed."""
    ok = [r for r in rows if r.get("status") == "ok"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        groups[r["group"]].append(r)

    def recall(rs: list[dict]) -> dict[str, Any]:
        return {k: mean([r["evidence"][k] for r in rs if k in r.get("evidence", {})]) for k in recall_keys}

    memory = [r.get("memory", {}) for r in ok]
    summary = common_summary(rows, None, stop, expected)
    summary.update({
        "benchmark": benchmark,
        "retrieval_only": True,
        **recall(ok),
        "by_group": {g: {"n": len(rs), **recall(rs)} for g, rs in sorted(groups.items())},
        "context_tokens_o200k": distribution([m["context_tokens_o200k"] for m in memory if "context_tokens_o200k" in m]),
        "joined_tokens_o200k": distribution([m["joined_tokens_o200k"] for m in memory if "joined_tokens_o200k" in m]),
        "context_tokens_heuristic": distribution([m["context_tokens_heuristic"] for m in memory
                                                  if "context_tokens_heuristic" in m]),
        "fill": distribution([m["fill"] for m in memory if m.get("fill") is not None]),
        "widened": distribution([m["widened"] for m in memory if "widened" in m]),
        "rank_ms": distribution([m["rank_ms"] for m in memory if "rank_ms" in m]),
        "passthrough_share": mean([1.0 if m.get("passthrough") else 0.0 for m in memory]),
        "prompt_tokens_o200k": distribution([r["prompt_tokens_o200k"] for r in ok]),
    })
    return summary
