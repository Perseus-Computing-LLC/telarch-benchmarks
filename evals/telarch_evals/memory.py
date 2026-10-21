"""Memory systems under test.

telarch       Kibisis stores every unit of a history (encrypted, one vault per history) and recalls
              candidates with its full-text search; Harpe picks what fits the token budget.
full-context  The history itself, newest first until the budget is full: the long-context baseline.
none          No history at all: the floor.

Every benchmark turns its data into `Unit`s and renders the selected units with its own prompt.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from .textutil import STOPWORDS_SHA256, keyword_terms


@dataclass
class Unit:
    uid: str                 # unique within one history
    session: str             # session the unit belongs to
    order: tuple             # chronological sort key
    date: str                # date string as the dataset gives it
    text: str                # what Kibisis stores and searches
    rendered: str            # what the reader sees for this unit
    turns: list[dict] = field(default_factory=list)
    timestamp: float = 0.0
    meta: dict = field(default_factory=dict)  # evidence markers for scoring; never stored in memory


@dataclass
class Recall:
    units: list[Unit]
    stats: dict[str, Any]


class MemorySystem(Protocol):
    name: str

    def config(self) -> dict[str, Any]: ...

    def open(self, history_id: str, units: list[Unit]) -> dict[str, Any]: ...

    def recall(self, history_id: str, question: str) -> Recall: ...

    def close(self, history_id: str) -> None: ...


def package_versions() -> dict[str, str]:
    out = {}
    for name in ("kibisis", "harpe", "argus-ledger", "scipy", "json-repair"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def _token_counter():
    from harpe import ContextBudgetEngine

    return ContextBudgetEngine().token_counter.count_tokens


def _o200k_counter():
    """Exact token counts for the reader: o200k_base is the gpt-4o-mini tokenizer."""
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    return lambda text: len(encoding.encode(text, disallowed_special=()))


READER_WINDOW = 128_000  # gpt-4o-mini-2024-07-18 context window, in o200k_base tokens
PROMPT_RESERVE = 4_000  # template, question, per-unit headers and chat framing; MemoryAgentBench's buffer_length


def full_context_budget(reader_max_tokens: int) -> int:
    """The most history, in reader tokens, that fits the reader's window beside its answer and the prompt."""
    return READER_WINDOW - reader_max_tokens - PROMPT_RESERVE


# The operator's own Kibisis settings. A benchmark never needs them, and without a passphrase
# argument Kibisis reads them, so a --plaintext vault would be encrypted with the operator's key.
KIBISIS_ENV = ("KIBISIS_PASSPHRASE", "KIBISIS_PASSPHRASE_FILE")
SEARCH_ENCRYPTED = ("decrypt and substring-match every entry in storage order; all hits score the same "
                    "(Kibisis keeps no plaintext in its full-text index when content is encrypted)")
SEARCH_PLAINTEXT = "SQLite FTS5, BM25 rank"


class TelarchMemory:
    name = "telarch"

    def __init__(self, workdir: Path, *, budget: int = 8000, candidates: int = 200, per_term: int = 50,
                 policy: str = "least_relevant", admission_gate: bool = False, encrypt: bool = True) -> None:
        from harpe import ContextBudgetEngine, PrunePolicy

        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.candidates = candidates
        self.per_term = per_term
        self.policy_name = policy
        self.admission_gate = admission_gate
        self.encrypt = encrypt
        self._engine = ContextBudgetEngine()
        self._count = self._engine.token_counter.count_tokens
        self._policy = PrunePolicy[policy.upper()]
        self._open: dict[str, tuple[Any, Path, dict[str, Unit]]] = {}
        for name in KIBISIS_ENV:
            os.environ.pop(name, None)

    def config(self) -> dict[str, Any]:
        return {
            "system": self.name,
            "budget_tokens": self.budget,
            "candidates": self.candidates,
            "per_term_limit": self.per_term,
            "harpe_policy": self.policy_name,
            "harpe_token_counter": type(self._engine.token_counter).__name__,
            "kibisis_encrypted": self.encrypt,
            "kibisis_search": SEARCH_ENCRYPTED if self.encrypt else SEARCH_PLAINTEXT,
            "kibisis_env_passphrase": "removed from the benchmark process",
            "kibisis_admission_gate": self.admission_gate,
            "query_planner": "content words of the question; one Kibisis search per word; per-word scores summed",
            "stopwords_sha256": STOPWORDS_SHA256,
        }

    @staticmethod
    def _remove(path: Path) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def open(self, history_id: str, units: list[Unit]) -> dict[str, Any]:
        from kibisis import SQLiteMemoryVault

        path = self.workdir / (hashlib.sha256(history_id.encode("utf-8")).hexdigest()[:20] + ".sqlite")
        self._remove(path)
        start = time.perf_counter()
        if self.encrypt:
            # A fresh random passphrase per history; it lives only in this process.
            vault = SQLiteMemoryVault(str(path), passphrase=secrets.token_urlsafe(32), auto_gate=self.admission_gate)
        else:
            vault = SQLiteMemoryVault(str(path), allow_plaintext=True, auto_gate=self.admission_gate)
        by_uid: dict[str, Unit] = {}
        items = []
        for unit in units:
            if not unit.text.strip():
                continue
            by_uid[unit.uid] = unit
            items.append({"category": "history", "key": unit.uid, "content": unit.text, "scope": "eval",
                          "epistemic_state": "verified", "metadata": {"session": unit.session, "date": unit.date}})
        errors = 0
        for i in range(0, len(items), 500):
            batch = items[i:i + 500]
            try:
                vault.put_batch(batch)
            except Exception:  # noqa: BLE001 - retry one by one so a single bad unit costs only itself
                for item in batch:
                    try:
                        vault.put_batch([item])
                    except Exception:  # noqa: BLE001 - counted in ingest_errors
                        errors += 1
                        by_uid.pop(item["key"], None)
        self._open[history_id] = (vault, path, by_uid)
        return {"units": len(by_uid), "ingest_errors": errors,
                "ingest_ms": round((time.perf_counter() - start) * 1000, 1)}

    def recall(self, history_id: str, question: str) -> Recall:
        from harpe import ContextItem, TokenBudget

        vault, _, by_uid = self._open[history_id]
        terms = keyword_terms(question)
        start = time.perf_counter()
        scores: dict[str, float] = {}
        hits = 0
        for term in terms:
            for res in vault.search(term, scope="eval", limit=self.per_term, include_unverified=True):
                key = res.entity.key
                if key in by_uid:
                    # FTS5 ranks by BM25 as a negative number; summing per-word scores gives the
                    # BM25 score of an OR query over the content words.
                    scores[key] = scores.get(key, 0.0) + max(0.0, -float(res.rank_score))
                    hits += 1
        searched = time.perf_counter()
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], by_uid[kv[0]].order))[: self.candidates]
        top = ranked[0][1] if ranked and ranked[0][1] > 0 else 1.0
        items = []
        for rank, (uid, score) in enumerate(ranked):
            unit = by_uid[uid]
            relevance = score / top
            # Priorities run 1 (most relevant) to 100. Harpe pins priority-0 items past the budget.
            items.append(ContextItem(
                id=uid, content=unit.rendered, token_count=self._count(unit.rendered),
                priority=1 + min(99, max(0, round(99 * (1 - relevance)))),
                created_at=unit.timestamp, metadata={"relevance": relevance, "rank": rank}))
        report = None
        if items:
            report = self._engine.synthesize(items, TokenBudget(total_budget=self.budget), policy=self._policy,
                                             query=question)
        done = time.perf_counter()
        chosen = [by_uid[item.id] for item in report.included_items] if report else []
        chosen.sort(key=lambda u: u.order)
        stats = {
            "terms": terms,
            "search_hits": hits,
            "candidates": len(items),
            "selected": len(chosen),
            "context_tokens": int(report.consumed_tokens) if report else 0,
            "pruned": len(report.pruned_items) if report else 0,
            "query_intent": report.query_intent if report else None,
            "search_ms": round((searched - start) * 1000, 2),
            "harpe_ms": round((done - searched) * 1000, 2),
        }
        return Recall(chosen, stats)

    def close(self, history_id: str) -> None:
        entry = self._open.pop(history_id, None)
        if entry:
            vault, path, _ = entry
            vault.close()
            self._remove(path)


class FullContextMemory:
    """The long-context baseline: the newest units that fit the budget, counted in the reader's own tokens."""

    name = "full-context"

    def __init__(self, *, budget: int = 100_000) -> None:
        self.budget = budget
        self._count = _o200k_counter()
        self._open: dict[str, list[Unit]] = {}

    def config(self) -> dict[str, Any]:
        return {"system": self.name, "budget_tokens": self.budget,
                "truncation": "keep the most recent units that fit the budget",
                "token_counter": "tiktoken o200k_base (gpt-4o-mini)"}

    def open(self, history_id: str, units: list[Unit]) -> dict[str, Any]:
        self._open[history_id] = sorted(units, key=lambda u: u.order)
        return {"units": len(units), "ingest_errors": 0, "ingest_ms": 0.0}

    def recall(self, history_id: str, question: str) -> Recall:
        units = self._open[history_id]
        chosen: list[Unit] = []
        used = 0
        for unit in reversed(units):
            size = self._count(unit.rendered)
            if used + size > self.budget:
                break
            chosen.append(unit)
            used += size
        chosen.reverse()
        return Recall(chosen, {"selected": len(chosen), "units_total": len(units), "context_tokens": used,
                               "truncated": len(chosen) < len(units)})

    def close(self, history_id: str) -> None:
        self._open.pop(history_id, None)


class NoMemory:
    name = "none"

    def config(self) -> dict[str, Any]:
        return {"system": self.name}

    def open(self, history_id: str, units: list[Unit]) -> dict[str, Any]:
        return {"units": len(units), "ingest_errors": 0, "ingest_ms": 0.0}

    def recall(self, history_id: str, question: str) -> Recall:
        return Recall([], {"selected": 0, "context_tokens": 0})

    def close(self, history_id: str) -> None:
        return None


def make_system(name: str, workdir: Path, *, budget: int | None, candidates: int, per_term: int, policy: str,
                admission_gate: bool, encrypt: bool, reader_max_tokens: int = 1024) -> MemorySystem:
    if name == "telarch":
        return TelarchMemory(workdir, budget=budget or 8000, candidates=candidates, per_term=per_term,
                             policy=policy, admission_gate=admission_gate, encrypt=encrypt)
    if name == "full-context":
        return FullContextMemory(budget=budget or full_context_budget(reader_max_tokens))
    if name == "none":
        return NoMemory()
    raise ValueError(f"unknown system {name!r}")
