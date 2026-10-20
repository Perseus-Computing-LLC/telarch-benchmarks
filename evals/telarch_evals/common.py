"""Shared pieces of a benchmark run: the context object and the summary helpers."""

from __future__ import annotations

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


def base_config(ctx: Context, extra: dict[str, Any]) -> dict[str, Any]:
    """Settings that define a run. A resumed run must match them exactly."""
    config = {"system": ctx.system.name, "memory": ctx.system.config(), "reader": ctx.reader.describe(),
              "judge": ctx.judge.describe()}
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
            "p50": round(percentile(values, 0.5) or 0, 2), "p95": round(percentile(values, 0.95) or 0, 2)}


def common_summary(rows: list[dict], meter: Meter | None, stop: str | None, expected: int) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok"]
    errors = [r for r in rows if r.get("status") != "ok"]
    retrieval = [r.get("memory", {}) for r in ok]
    summary: dict[str, Any] = {
        "rows": len(ok), "errors": len(errors), "expected": expected,
        "complete": len(ok) == expected and not errors and stop is None,
        "stopped": stop,
        "context_tokens": distribution([m["context_tokens"] for m in retrieval if "context_tokens" in m]),
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
