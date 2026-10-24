"""Memory systems under test.

telarch       Kibisis stores every unit of a history (encrypted, one vault per history) and recalls
              candidates with its full-text search; Harpe picks what fits the token budget.
full-context  The history itself, newest first until the budget is full: the long-context baseline.
none          No history at all: the floor.

Every benchmark turns its data into `Unit`s and renders the selected units with its own prompt.
"""

from __future__ import annotations

import bisect
import hashlib
import inspect
import json
import math
import os
import re
import secrets
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from .textutil import _WORD, STOPWORDS_SHA256, keyword_terms


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


def package_sources() -> dict[str, dict[str, Any]]:
    """Version and origin of each library under test: a package index release, a git URL and commit, or an
    editable checkout and its commit, so that a run names the exact code it measured."""
    out: dict[str, dict[str, Any]] = {}
    for name in ("kibisis", "harpe", "argus-ledger"):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            out[name] = {"version": None, "source": None}
            continue
        entry: dict[str, Any] = {"version": dist.version, "source": "index"}
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            vcs = info.get("vcs_info") or {}
            if vcs:
                entry.update(source="vcs", url=info.get("url"), commit=vcs.get("commit_id"),
                             ref=vcs.get("requested_revision"))
            elif (info.get("dir_info") or {}).get("editable"):
                path = str(info.get("url", "")).removeprefix("file://")
                try:
                    sha = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True,
                                         timeout=10).stdout.strip() or None
                except (OSError, subprocess.SubprocessError):
                    sha = None
                entry.update(source="editable", path=path, commit=sha)
            else:
                entry.update(source="url", url=info.get("url"))
        out[name] = entry
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
                    "(Kibisis before 0.1.0a7 keeps no plaintext in its full-text index when content is encrypted)")
SEARCH_ENCRYPTED_RANKED = "SQLite FTS5, BM25 rank, over Kibisis's entry index inside the SQLCipher file (0.1.0a7+)"
SEARCH_PLAINTEXT = "SQLite FTS5, BM25 rank"
SEARCH_KEYS_ONLY = "; keys and scores only, no entry opened (search_keys, 0.1.0a8+)"
PLANNERS = {
    "fanout": "content words of the question; one Kibisis search per word; per-word scores summed",
    "any": "content words of the question; one Kibisis any-word BM25 search (match='any')",
}
COUNTERS = ("o200k", "heuristic")
MAX_WIDEN = 4           # candidate-floor doublings per question
MAX_PER_TERM = 400
MAX_CANDIDATES = 800
BRIDGE_TERMS = 6        # second-hop search words taken from the top hits
BRIDGE_WEIGHT = 0.5     # second-hop hits count half
_NAME = re.compile(r"\b[A-Z][a-z]{2,}\b")
_WORD_ANYCASE = re.compile(_WORD.pattern, re.IGNORECASE)
JOIN = "\n\n"  # how BEAM's reader prompt joins units (bench_beam.render): --window keeps the joined pack in budget


class _Messages:
    """One history's turns for --window, as round 6's D6 re-pack (w<W>/kib) cut them: each message as the reader
    sees it (its unit's date line, if the unit shows one, then `role: text`), and a message over W o200k tokens
    cut to one W-token window. Of the windows centred on an occurrence of a question word (clamped to the
    message), it keeps the one with the most question-word weight: the sum of the idf of the distinct question
    words in it, over message-level document frequencies; the earliest on ties, the head when none occurs."""

    def __init__(self, units: list[Unit], encoding: Any) -> None:
        self._enc = encoding
        self._cache: dict[str, list[dict]] = {}
        self._df: Counter = Counter()
        self._n = 0
        for unit in units:
            for turn in unit.turns:
                self._df.update(set(_WORD.findall(str(turn.get("content", "")).strip().lower())))
                self._n += 1

    def idf(self, word: str) -> float:
        d = self._df.get(word, 0)
        return math.log(1 + (self._n - d + 0.5) / (d + 0.5))

    def _of(self, unit: Unit) -> list[dict]:
        ms = self._cache.get(unit.uid)
        if ms is None:
            head = unit.rendered[: len(unit.rendered) - len(unit.text)] if unit.rendered.endswith(unit.text) else ""
            ms = self._cache[unit.uid] = []
            for turn in unit.turns:
                text = str(turn.get("content", "")).strip()
                ms.append({"turn": turn, "text": text, "prefix": f"{head}{turn.get('role', 'user')}: ",
                           "toks": self._enc.encode(text, disallowed_special=()), "pos": None})
        return ms

    def _positions(self, m: dict) -> list[tuple[int, str]]:
        """(token index, lowercased word) for every word of a message, by UTF-8 byte offset."""
        if m["pos"] is None:
            starts, p = [], 0
            for t in m["toks"]:
                starts.append(p)
                p += len(self._enc.decode_single_token_bytes(t))
            byte_at, acc = [], 0
            for ch in m["text"]:
                byte_at.append(acc)
                acc += len(ch.encode("utf-8"))
            m["pos"] = [(bisect.bisect_right(starts, byte_at[x.start()]) - 1, x.group(0).lower())
                        for x in _WORD_ANYCASE.finditer(m["text"])]
        return m["pos"]

    def items(self, unit: Unit, size: int, weights: dict[str, float]) -> list[tuple[dict, str, str]]:
        """(turn, text, rendered) of each message of `unit`, a message over `size` tokens cut to its window."""
        out = []
        for m in self._of(unit):
            n = len(m["toks"])
            if n <= size:
                out.append((m["turn"], m["text"], m["prefix"] + m["text"]))
                continue
            hits = [(ti, w) for ti, w in self._positions(m) if w in weights]
            best, start = -1.0, 0
            for ti, _ in hits:
                s = min(max(0, ti - size // 2), n - size)
                weight = sum(weights[w] for w in {w for tj, w in hits if s <= tj < s + size})
                if weight > best:
                    best, start = weight, s
            body = self._enc.decode_bytes(m["toks"][start:start + size]).decode("utf-8", errors="ignore").strip()
            out.append((m["turn"], body, m["prefix"] + ("… " if start > 0 else "") + body
                        + (" …" if start + size < n else "")))
        return out


def kibisis_searches_keys() -> bool:
    """kibisis 0.1.0a8 added search_keys(): keys and scores without opening (or decrypting) entries."""
    from kibisis import SQLiteMemoryVault

    return hasattr(SQLiteMemoryVault, "search_keys")


def kibisis_sets_cache() -> bool:
    """kibisis 0.1.0a9 added SQLiteMemoryVault(cache_mb=...): the SQLite page cache in MiB."""
    import inspect

    from kibisis import SQLiteMemoryVault

    return "cache_mb" in inspect.signature(SQLiteMemoryVault.__init__).parameters


def kibisis_ranks_sealed_entries() -> bool:
    """kibisis 0.1.0a7 ranks sealed entries and added search(match=...)."""
    from kibisis import SQLiteMemoryVault

    return "match" in inspect.signature(SQLiteMemoryVault.search).parameters


class ReaderTokens:
    """Harpe token counter in the reader's tokens (tiktoken o200k_base, the gpt-4o-mini tokenizer)."""

    def __init__(self) -> None:
        self.count_tokens = _o200k_counter()


class TelarchMemory:
    name = "telarch"

    def __init__(self, workdir: Path, *, budget: int = 8000, candidates: int = 200, per_term: int = 50,
                 policy: str = "least_relevant", admission_gate: bool = False, encrypt: bool = True,
                 counter: str = "o200k", planner: str = "fanout", fill_floor: float = 0.0,
                 passthrough_under: int = 0, recency: float = 0.0, mmr: float = 0.0, bridge: int = 0,
                 memory_security: bool = True, resolve_dates: bool = False, surface_conflicts: bool = False,
                 cache_mb: int | None = None, window: int = 0) -> None:
        from harpe import ContextBudgetEngine, PrunePolicy

        try:
            from harpe import route_by_fit  # noqa: F401 - harpe 0.1.0a5 added fit-aware routing
        except ImportError as exc:
            raise ValueError("the telarch system needs harpe 0.1.0a5 or later (route_by_fit)") from exc

        if counter not in COUNTERS:
            raise ValueError(f"counter must be one of {', '.join(COUNTERS)}")
        if planner not in PLANNERS:
            raise ValueError(f"planner must be one of {', '.join(PLANNERS)}")
        if planner == "any" and not kibisis_ranks_sealed_entries():
            raise ValueError("planner 'any' needs kibisis 0.1.0a7 or later (search(match='any'))")
        if not kibisis_searches_keys():
            raise ValueError("the telarch system needs kibisis 0.1.0a8 or later (search_keys)")
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.candidates = candidates
        self.per_term = per_term
        self.policy_name = policy
        self.admission_gate = admission_gate
        self.encrypt = encrypt
        self.counter = counter
        self.planner = planner
        self.fill_floor = fill_floor
        self.passthrough_under = passthrough_under
        self.recency = recency
        self.mmr = mmr
        self.bridge = bridge
        self.memory_security = memory_security
        self.resolve_dates = resolve_dates
        self.surface_conflicts = surface_conflicts
        self.window = int(window) if window else 0
        if cache_mb is not None:
            if int(cache_mb) <= 0:
                raise ValueError("--cache-mb must be a positive number of MiB")
            if not kibisis_sets_cache():
                raise ValueError("--cache-mb needs kibisis 0.1.0a9 or later (cache_mb)")
        self.cache_mb = int(cache_mb) if cache_mb is not None else None
        if surface_conflicts:
            try:
                from harpe import find_conflicts  # noqa: F401 - harpe 0.1.0a6 added it
            except ImportError as exc:
                raise ValueError("--surface-conflicts needs harpe 0.1.0a6 or later (find_conflicts)") from exc
        if resolve_dates:
            try:
                from harpe import resolve_relative_dates  # noqa: F401 - harpe 0.1.0a6 added it
            except ImportError as exc:
                raise ValueError("--resolve-dates needs harpe 0.1.0a6 or later (resolve_relative_dates)") from exc
        self._o200k = _o200k_counter()
        self._heuristic = _token_counter()
        self._reader_tokens = ReaderTokens()
        # Harpe counts budget in the reader's tokens; its own heuristic counts about 15% more on these histories.
        self._engine = ContextBudgetEngine(token_counter=ReaderTokens()) if counter == "o200k" else ContextBudgetEngine()
        self._policy = PrunePolicy[policy.upper()]
        import tiktoken
        self._enc = tiktoken.get_encoding("o200k_base")
        self._open: dict[str, tuple[Any, Path, dict[str, Unit]]] = {}
        self._history: dict[str, list[Unit]] = {}
        self._whole: dict[str, int | None] = {}
        self._sizes: dict[str, dict[str, dict[str, int]]] = {}
        self._bags: dict[str, dict[str, frozenset]] = {}
        self._messages: dict[str, _Messages] = {}
        for name in KIBISIS_ENV:
            os.environ.pop(name, None)

    def config(self) -> dict[str, Any]:
        if self.encrypt:
            search = SEARCH_ENCRYPTED_RANKED if kibisis_ranks_sealed_entries() else SEARCH_ENCRYPTED
        else:
            search = SEARCH_PLAINTEXT
        search += SEARCH_KEYS_ONLY
        return {
            "system": self.name,
            "budget_tokens": self.budget,
            "candidates": self.candidates,
            "per_term_limit": self.per_term,
            "harpe_policy": self.policy_name,
            "harpe_token_counter": ("tiktoken o200k_base" if self.counter == "o200k"
                                    else type(self._engine.token_counter).__name__),
            "kibisis_encrypted": self.encrypt,
            # SQLCipher's cipher_memory_security (process-wide): kibisis turns it on unless memory_security=False.
            "kibisis_memory_security": self.memory_security if self.encrypt else None,
            # SQLite page cache per vault in MiB (kibisis cache_mb, 0.1.0a9+); None = SQLite's default of a few MiB.
            # Under SQLCipher the cache holds decrypted pages, so a page read again is not decrypted again.
            "kibisis_cache_mb": self.cache_mb,
            "kibisis_search": search,
            "kibisis_env_passphrase": "removed from the benchmark process",
            "kibisis_admission_gate": self.admission_gate,
            "query_planner": PLANNERS[self.planner],
            "stopwords_sha256": STOPWORDS_SHA256,
            "fill_floor": self.fill_floor,
            "passthrough_under_tokens": self.passthrough_under,
            "recency": self.recency,
            "mmr": self.mmr,
            "bridge": self.bridge,
            # What the reader sees: relative dates ("yesterday") gain the calendar date of the turn's session.
            "resolve_relative_dates": self.resolve_dates,
            # A denial and a statement it contradicts, both selected, get a note after the denial (harpe find_conflicts).
            "surface_conflicts": self.surface_conflicts,
            **({"window": self.window} if self.window else {}),
        }

    @staticmethod
    def _remove(path: Path) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    def open(self, history_id: str, units: list[Unit]) -> dict[str, Any]:
        from harpe import ContextRoute, route_by_fit
        from kibisis import SQLiteMemoryVault

        path = self.workdir / (hashlib.sha256(history_id.encode("utf-8")).hexdigest()[:20] + ".sqlite")
        self._remove(path)
        start = time.perf_counter()
        cache = {"cache_mb": self.cache_mb} if self.cache_mb is not None else {}
        if self.encrypt:
            # A fresh random passphrase per history; it lives only in this process.
            vault = SQLiteMemoryVault(str(path), passphrase=secrets.token_urlsafe(32), auto_gate=self.admission_gate,
                                      memory_security=self.memory_security, **cache)
        else:
            vault = SQLiteMemoryVault(str(path), allow_plaintext=True, auto_gate=self.admission_gate, **cache)
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
        if self.resolve_dates:  # after ingest: Kibisis stores and searches the turns as they were said
            units = [self._dated(u) for u in units]
            by_uid.update({u.uid: u for u in units if u.uid in by_uid})
        self._open[history_id] = (vault, path, by_uid)
        # Units in the order the full-context arm renders them: a passed-through history gets the same prompt.
        self._history[history_id] = sorted(units, key=lambda u: u.order)
        self._sizes[history_id] = {}
        self._bags[history_id] = {}
        if self.window > 0:
            self._messages[history_id] = _Messages(units, self._enc)
        # Fit-aware routing (harpe 0.1.0a5+), in the reader's tokens: a history under the threshold goes whole.
        decision = route_by_fit((unit.rendered for unit in self._history[history_id]), self.passthrough_under,
                                self._reader_tokens)
        self._whole[history_id] = decision.whole_tokens if decision.route is ContextRoute.WHOLE else None
        return {"units": len(by_uid), "ingest_errors": errors,
                "ingest_ms": round((time.perf_counter() - start) * 1000, 1)}

    def add(self, history_id: str, units: list[Unit]) -> dict[str, Any]:
        """Ingest more units into an open history. LongMemEval-V2 inserts its haystack one trajectory at a time,
        so the index is built while inserting and a query only searches."""
        vault, _, by_uid = self._open[history_id]
        start = time.perf_counter()
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
        if self.resolve_dates:
            units = [self._dated(u) for u in units]
            by_uid.update({u.uid: u for u in units if u.uid in by_uid})
        self._history[history_id] = sorted(self._history[history_id] + list(units), key=lambda u: u.order)
        self._whole[history_id] = None  # passthrough applies only to histories opened whole
        return {"units": len(items) - errors, "ingest_errors": errors,
                "ingest_ms": round((time.perf_counter() - start) * 1000, 1)}

    @staticmethod
    def _dated(unit: Unit) -> Unit:
        """The unit as the reader sees it with --resolve-dates: relative dates resolved against its session date."""
        from harpe import resolve_relative_dates

        text = resolve_relative_dates(unit.text, unit.date)
        if text == unit.text:
            return unit
        return replace(unit, text=text, rendered=resolve_relative_dates(unit.rendered, unit.date),
                       turns=[{**t, "content": resolve_relative_dates(str(t.get("content", "")), unit.date)}
                              for t in unit.turns], meta={**unit.meta, "dated": True})

    @staticmethod
    def _with_conflicts(units: list[Unit], question: str) -> tuple[list[Unit], int]:
        """--surface-conflicts: after each denying sentence that another selected unit contradicts, a note quoting
        the other statement (and its date). Notes add a few tokens beyond the packing budget."""
        from harpe import conflict_note, find_conflicts

        conflicts = find_conflicts({u.uid: u.text for u in units}, query=question)
        if not conflicts:
            return units, 0
        by_uid = {u.uid: u for u in units}
        noted = 0
        for c in conflicts:
            unit = by_uid[c.denied_id]
            if c.denied not in unit.text:
                continue
            note = f"{c.denied} {conflict_note(c, by_uid[c.stated_id].date or None)}"
            by_uid[c.denied_id] = replace(
                unit, text=unit.text.replace(c.denied, note, 1), rendered=unit.rendered.replace(c.denied, note, 1),
                turns=[{**t, "content": str(t.get("content", "")).replace(c.denied, note, 1)} for t in unit.turns],
                meta={**unit.meta, "conflict_note": True})
            noted += 1
        return [by_uid[u.uid] for u in units], noted

    def _size(self, history_id: str, unit: Unit, which: str) -> int:
        cache = self._sizes[history_id].setdefault(which, {})
        size = cache.get(unit.uid)
        if size is None:
            size = cache[unit.uid] = (self._o200k if which == "o200k" else self._heuristic)(unit.rendered)
        return size

    def _search(self, vault: Any, by_uid: dict[str, Unit], terms: list[str], per_term: int, limit: int,
                scores: dict[str, float], weight: float = 1.0) -> tuple[int, int, bool]:
        """Add Kibisis BM25 scores (higher is better) to `scores`. Returns hits, searches, and whether a
        search filled its limit (so a wider one could find more)."""
        if not terms:
            return 0, 0, False
        # Keys-only search (kibisis 0.1.0a8+): the harness holds every unit's text, so opening (and, in an
        # encrypted vault, decrypting) each result would only cost time.
        if self.planner == "any":
            groups = [vault.search_keys(" ".join(terms), scope="eval", limit=limit, include_unverified=True, match="any")]
            full = len(groups[0]) >= limit
        else:
            groups = [vault.search_keys(term, scope="eval", limit=per_term, include_unverified=True) for term in terms]
            full = any(len(g) >= per_term for g in groups)
        hits = 0
        for results in groups:
            for res in results:
                key = res.key
                if key in by_uid:
                    # FTS5 ranks by BM25 as a negative number; summing per-word scores gives the
                    # BM25 score of an OR query over the content words.
                    scores[key] = scores.get(key, 0.0) + weight * max(0.0, -float(res.rank_score))
                    hits += 1
        return hits, len(groups), full

    def _bridge_terms(self, by_uid: dict[str, Unit], scores: dict[str, float], asked: list[str]) -> list[str]:
        """Names in the top hits that the question does not use: the second hop of a 2-hop question."""
        top = sorted(scores.items(), key=lambda kv: (-kv[1], by_uid[kv[0]].order))[: self.bridge]
        counts: dict[str, int] = {}
        for uid, _ in top:
            for word in keyword_terms(" ".join(_NAME.findall(by_uid[uid].text))):
                if word not in asked:
                    counts[word] = counts.get(word, 0) + 1
        return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:BRIDGE_TERMS]

    def _bag(self, history_id: str, unit: Unit) -> frozenset:
        bags = self._bags[history_id]
        bag = bags.get(unit.uid)
        if bag is None:
            bag = bags[unit.uid] = frozenset(keyword_terms(unit.text))
        return bag

    def _rank(self, history_id: str, scores: dict[str, float], by_uid: dict[str, Unit],
              limit: int) -> list[tuple[str, float]]:
        """(uid, relevance from 0 to 1), best first: BM25, then the optional recency boost and MMR diversity."""
        if not scores:
            return []
        adjusted = scores
        if self.recency > 0:
            history = self._history[history_id]
            last = max(1, len(history) - 1)
            position = {u.uid: i / last for i, u in enumerate(history)}
            adjusted = {uid: s * (1.0 + self.recency * position.get(uid, 0.0)) for uid, s in scores.items()}
        ranked = sorted(adjusted.items(), key=lambda kv: (-kv[1], by_uid[kv[0]].order))[:limit]
        top = ranked[0][1] if ranked[0][1] > 0 else 1.0
        ranked = [(uid, s / top) for uid, s in ranked]
        if self.mmr > 0 and len(ranked) > 2:
            # Maximal marginal relevance: relevance minus `mmr` x the highest word overlap (Jaccard) with an
            # entry already taken, so near-duplicates drop below distinct matches.
            index = {uid: i for i, (uid, _) in enumerate(ranked)}
            pool = dict(ranked)
            overlap = {uid: 0.0 for uid in pool}
            order: list[tuple[str, float]] = []
            while pool:
                uid = max(pool, key=lambda u: (pool[u] - self.mmr * overlap[u], -index[u]))
                value = pool.pop(uid) - self.mmr * overlap.pop(uid)
                order.append((uid, max(0.0, value)))
                bag = self._bag(history_id, by_uid[uid])
                for other in pool:
                    theirs = self._bag(history_id, by_uid[other])
                    union = len(bag | theirs)
                    if union:
                        overlap[other] = max(overlap[other], len(bag & theirs) / union)
            ranked = order
        return ranked

    def _pack(self, history_id: str, ranked: list[tuple[str, float]], by_uid: dict[str, Unit], question: str):
        from harpe import ContextItem, TokenBudget

        items = []
        if self.window > 0 and history_id in self._messages:
            msg_helper = self._messages[history_id]
            terms = keyword_terms(question)
            weights = {w: msg_helper.idf(w) for w in terms}
            for rank, (uid, relevance) in enumerate(ranked):
                unit = by_uid[uid]
                msg_items = msg_helper.items(unit, self.window, weights)
                prio = 1 + min(99, max(0, round(99 * (1 - relevance))))
                for j, (turn, body, rendered) in enumerate(msg_items):
                    tok_count = len(self._enc.encode(rendered, disallowed_special=()))
                    items.append(ContextItem(
                        id=f"{uid}#{j}", content=rendered, token_count=tok_count,
                        priority=prio, created_at=unit.timestamp,
                        metadata={"relevance": relevance, "rank": rank, "turn": j, "uid": uid}))
            pack_budget = min(self.budget, 7900)
        else:
            for rank, (uid, relevance) in enumerate(ranked):
                unit = by_uid[uid]
                # Priorities run 1 (most relevant) to 100. Harpe pins priority-0 items past the budget.
                items.append(ContextItem(
                    id=uid, content=unit.rendered, token_count=self._size(history_id, unit, self.counter),
                    priority=1 + min(99, max(0, round(99 * (1 - relevance)))),
                    created_at=unit.timestamp, metadata={"relevance": relevance, "rank": rank}))
            pack_budget = self.budget
        if not items:
            return None, 0
        report = self._engine.synthesize(items, TokenBudget(total_budget=pack_budget), policy=self._policy,
                                         query=question)
        return report, len(items)

    def recall(self, history_id: str, question: str) -> Recall:
        vault, _, by_uid = self._open[history_id]
        whole = self._whole.get(history_id)
        if whole is not None:
            # Small enough to hand over whole: exactly the units and order the full-context arm uses.
            units = list(self._history[history_id])
            notes = 0
            if self.surface_conflicts:
                units, notes = self._with_conflicts(units, question)
            return Recall(units, {"passthrough": True, "selected": len(units), "units_total": len(units),
                                  "conflict_notes": notes,
                                  "dated_units": sum(1 for u in units if u.meta.get("dated")),
                                  "context_tokens": whole, "context_tokens_o200k": whole})
        terms = keyword_terms(question)
        per_term, limit = self.per_term, self.candidates
        widened = hits = searches = 0
        search_s = rank_s = harpe_s = 0.0
        bridge_terms: list[str] = []
        while True:
            t0 = time.perf_counter()
            scores: dict[str, float] = {}
            hits, searches_now, full = self._search(vault, by_uid, terms, per_term, limit, scores)
            searches += searches_now
            if self.bridge and scores:
                bridge_terms = self._bridge_terms(by_uid, scores, terms)
                more, n, _ = self._search(vault, by_uid, bridge_terms, per_term, limit, scores, weight=BRIDGE_WEIGHT)
                hits += more
                searches += n
            t1 = time.perf_counter()
            ranked = self._rank(history_id, scores, by_uid, limit)
            t2 = time.perf_counter()
            report, n_items = self._pack(history_id, ranked, by_uid, question)
            t3 = time.perf_counter()
            search_s += t1 - t0
            rank_s += t2 - t1
            harpe_s += t3 - t2
            consumed = int(report.consumed_tokens) if report else 0
            pack_budget = min(self.budget, 7900) if self.window > 0 else self.budget
            # Candidate floor: while Harpe fills less than `fill_floor` of the budget, widen retrieval, first
            # the per-word limit, then the candidate cut, as long as a wider search can find more.
            grow_terms = self.planner == "fanout" and full and per_term < MAX_PER_TERM
            grow_limit = (len(scores) > limit or (self.planner == "any" and full)) and limit < MAX_CANDIDATES
            if (self.fill_floor <= 0 or consumed >= self.fill_floor * pack_budget or widened >= MAX_WIDEN
                    or not (grow_terms or grow_limit)):
                break
            widened += 1
            if grow_terms:
                per_term = min(MAX_PER_TERM, per_term * 2)
            if grow_limit:
                limit = min(MAX_CANDIDATES, limit * 2)
        if report and self.window > 0:
            chosen = []
            for item in report.included_items:
                uid, _, j_str = item.id.partition("#")
                j = int(j_str) if j_str else 0
                base = by_uid[uid]
                chosen.append(Unit(
                    uid=item.id,
                    session=base.session,
                    order=(*base.order, j),
                    date=base.date,
                    text=item.content,
                    rendered=item.content,
                    meta={**base.meta, "parent_uid": uid, "turn_index": j, "window": True},
                ))
        else:
            chosen = [by_uid[item.id] for item in report.included_items] if report else []
        chosen.sort(key=lambda u: u.order)
        notes = 0
        if self.surface_conflicts:
            chosen, notes = self._with_conflicts(chosen, question)
        stats = {
            "terms": terms,
            "search_hits": hits,
            "searches": searches,
            "candidates": n_items,
            "selected": len(chosen),
            "context_tokens": consumed,
            "context_tokens_o200k": sum(self._size(history_id, u, "o200k") for u in chosen),
            "context_tokens_heuristic": sum(self._size(history_id, u, "heuristic") for u in chosen),
            "fill": round(consumed / self.budget, 4) if self.budget else None,
            "widened": widened,
            "per_term_used": per_term,
            "candidates_limit_used": limit,
            "pruned": len(report.pruned_items) if report else 0,
            "query_intent": report.query_intent if report else None,
            "dated_units": sum(1 for u in chosen if u.meta.get("dated")),
            "conflict_notes": notes,
            "search_ms": round(search_s * 1000, 2),
            "rank_ms": round(rank_s * 1000, 2),
            "harpe_ms": round(harpe_s * 1000, 2),
        }
        if self.bridge:
            stats["bridge_terms"] = bridge_terms
        if self.passthrough_under:
            stats["passthrough"] = False
        return Recall(chosen, stats)

    def close(self, history_id: str) -> None:
        entry = self._open.pop(history_id, None)
        for cache in (self._history, self._whole, self._sizes, self._bags, self._messages):
            cache.pop(history_id, None)
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
                admission_gate: bool, encrypt: bool, reader_max_tokens: int = 1024, **telarch: Any) -> MemorySystem:
    """`telarch` takes the round-2 options of TelarchMemory: counter, planner, fill_floor, passthrough_under,
    recency, mmr and bridge, and memory_security (kibisis 0.1.0a8+)."""
    if name == "telarch":
        return TelarchMemory(workdir, budget=budget or 8000, candidates=candidates, per_term=per_term,
                             policy=policy, admission_gate=admission_gate, encrypt=encrypt, **telarch)
    if name == "full-context":
        return FullContextMemory(budget=budget or full_context_budget(reader_max_tokens))
    if name == "none":
        return NoMemory()
    raise ValueError(f"unknown system {name!r}")
