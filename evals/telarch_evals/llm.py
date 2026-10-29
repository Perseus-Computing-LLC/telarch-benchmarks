"""OpenAI-compatible chat client with a response cache, retries and token accounting.

The API key is read from an environment variable (or a dotenv file) by name. It is sent only in the
Authorization header and never written to manifests, logs or results.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .sources import USER_AGENT

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# USD per million tokens (input, output): list prices on PRICES_DATE. Dollar figures in a summary
# are estimates from this table; check the provider's price list before quoting one.
PRICES_DATE = "2026-10-20"
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-2024-05-13": (5.00, 15.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-2025-04-14": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-mini-2025-04-14": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-nano-2025-04-14": (0.10, 0.40),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}

_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{24,}")


class LLMError(RuntimeError):
    pass


def scrub(text: str) -> str:
    """Remove long token-like strings and URL credentials from text bound for logs."""
    text = re.sub(r"//[^/\s@]+@", "//***@", text)
    return _SECRETISH.sub("<r>", text)


def scrub_url(url: str) -> str:
    return re.sub(r"//[^/\s@]+@", "//***@", url)


def read_key(name: str, env_file: str | None = None) -> str | None:
    """Value of the variable `name` from the environment, else from a dotenv file. NONE means keyless."""
    if name.upper() == "NONE":
        return None
    value = os.environ.get(name)
    if value:
        return value
    if env_file:
        pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)$")
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                match = pattern.match(line.rstrip("\n"))
                if match:
                    value = match.group(1).strip().strip('"').strip("'")
                    if value:
                        return value
    raise LLMError(f"no API key: set {name} in the environment or pass --env-file with a {name}= line")


_COUNTS = ("requests", "cache_hits", "prompt_tokens", "completion_tokens")
_REPORTED = "reported_usd"  # sum of gateway-reported usage.cost (USD) for billed requests, when the gateway sends one


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _reported_cost(usage: dict) -> float | None:
    """``usage.cost`` as USD when the gateway sent a number (0 counts: a free model is priced, at zero)."""
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost) if cost >= 0 else None


class Meter:
    """Token counts per model, split into billed requests and cache hits, and the spend.

    Spend per model comes from the gateway when it reports one (``usage.cost`` in USD per request, as the
    Nous Portal inference API does, promos applied) — that is the bill — and from the price table otherwise.
    A model with neither stays in ``unpriced_models`` so the summary says so instead of showing $0.

    `track(path)` keeps the counts of one run at `path` (its spend.json): it loads what earlier processes of
    the run left there and rewrites the file atomically after every call, so the totals and the --max-usd
    check of a resumed run cover the whole run.
    """

    def __init__(self, prices: dict[str, tuple[float, float]] | None = None) -> None:
        self._lock = threading.Lock()
        self.by_model: dict[str, dict[str, int]] = {}
        self.prices = dict(PRICES, **(prices or {}))
        self._path: Path | None = None

    def track(self, path: Path) -> None:
        with self._lock:
            if path == self._path:
                return
            if self._path:  # another run: the counts so far are in that run's spend.json
                self.by_model = {}
            if path.exists():
                for model, counts in json.loads(path.read_text("utf-8"))["by_model"].items():
                    entry = self.by_model.setdefault(model, dict.fromkeys(_COUNTS, 0))
                    for name in _COUNTS:
                        entry[name] += int(counts.get(name, 0))
                    if _REPORTED in counts:
                        entry[_REPORTED] = entry.get(_REPORTED, 0.0) + float(counts[_REPORTED])
            self._path = path

    def add(self, model: str, prompt_tokens: int, completion_tokens: int, cached: bool,
            reported_usd: float | None = None) -> None:
        with self._lock:
            entry = self.by_model.setdefault(model, dict.fromkeys(_COUNTS, 0))
            if cached:
                entry["cache_hits"] += 1
            else:
                entry["requests"] += 1
                entry["prompt_tokens"] += prompt_tokens
                entry["completion_tokens"] += completion_tokens
                if reported_usd is not None:
                    entry[_REPORTED] = entry.get(_REPORTED, 0.0) + reported_usd
            if self._path:
                _write_atomic(self._path, json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n")

    def usd(self) -> float:
        """Spend of billed requests: gateway-reported where the gateway reported it, else the price table."""
        with self._lock:
            return self._usd()

    def _priced_by(self, model: str) -> str | None:
        if _REPORTED in self.by_model[model]:
            return "reported"
        if model in self.prices:
            return "table"
        return None

    def _usd(self) -> float:
        total = 0.0
        for model, entry in self.by_model.items():
            how = self._priced_by(model)
            if how == "reported":
                total += entry[_REPORTED]
            elif how == "table":
                price = self.prices[model]
                total += entry["prompt_tokens"] / 1e6 * price[0] + entry["completion_tokens"] / 1e6 * price[1]
        return total

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        models = {m: dict(e) for m, e in self.by_model.items()}
        priced_by = {m: how for m in sorted(models) if (how := self._priced_by(m))}
        return {
            "by_model": models,
            "estimated_usd": round(self._usd(), 4),
            "reported_usd": round(sum(e[_REPORTED] for e in models.values() if _REPORTED in e), 6),
            "priced_by": priced_by,
            "unpriced_models": sorted(m for m in models if m not in priced_by),
            "usd_per_million_tokens": {m: list(self.prices[m]) for m in sorted(models) if m in self.prices},
            "price_table_date": PRICES_DATE,
            "price_note": "estimate from the list-price table in telarch_evals/llm.py",
        }


class Cache:
    """Responses keyed by a hash of the full request, so a rerun or a resumed run is not billed twice.

    A stored response is never replaced: when two calls race on one key, the first stored wins and both get
    it. A run holds an exclusive lock on `<path>.lock` while it has the cache open, so a second run on the
    same cache stops at once instead of interleaving with it.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._held = os.fdopen(os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600), "r+")
        try:
            fcntl.flock(self._held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._held.read().strip()
            self._held.close()
            raise SystemExit(f"{path} is in use by another run{f' (pid {holder})' if holder else ''}; wait for it "
                             "to finish, pass --no-cache, or set TELARCH_EVALS_HOME to another directory") from None
        self._held.truncate(0)
        self._held.write(str(os.getpid()))
        self._held.flush()
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)")
            self._conn.commit()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM responses WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: dict) -> dict:
        """Store `body` unless the key already has a response; return the stored one."""
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO responses VALUES (?, ?, ?)",
                               (key, json.dumps(body, ensure_ascii=False), time.time()))
            self._conn.commit()
            row = self._conn.execute("SELECT body FROM responses WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0])

    def close(self) -> None:
        self._conn.close()
        self._held.close()  # releases the lock


def _reasoning_model(model: str) -> bool:
    name = model.split("/")[-1]
    return bool(re.match(r"^(o\d|gpt-5)", name))


class ChatClient:
    def __init__(self, model: str, *, base_url: str | None = None, api_key_env: str = "OPENAI_API_KEY",
                 env_file: str | None = None, temperature: float = 0.0, max_tokens: int = 1024,
                 cache: Cache | None = None, meter: Meter | None = None, timeout: float = 300.0,
                 retries: int = 8, extra: dict[str, Any] | None = None, cache_salt: str | None = None) -> None:
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = api_key_env
        self._key = read_key(api_key_env, env_file)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.cache = cache
        self.meter = meter or Meter()
        self.timeout = timeout
        self.retries = retries
        self.extra = dict(extra or {})  # further request fields, such as top_p and seed
        # A sampled completion (temperature > 0) is one draw, not the answer to the request. Caching it
        # under the request alone would hand every later run the same draw and call it a repeat
        # measurement, so sampled requests are keyed with a salt that is fresh per run. A resumed run
        # passes the salt from its manifest, so its retries see the draws it already paid for.
        self.cache_salt = cache_salt or secrets.token_hex(8)

    def derive(self, model: str, *, temperature: float | None = None, max_tokens: int | None = None,
               extra: dict[str, Any] | None = None) -> ChatClient:
        """A client for another model on the same endpoint, key, cache and meter."""
        clone = copy.copy(self)
        clone.model = model
        clone.temperature = self.temperature if temperature is None else temperature
        clone.max_tokens = self.max_tokens if max_tokens is None else max_tokens
        clone.extra = dict(extra or {})
        return clone

    def samples(self) -> bool:
        """True when the request carries a temperature above 0, so each response is one draw."""
        return not _reasoning_model(self.model) and self.temperature > 0

    def describe(self) -> dict[str, Any]:
        out = {"model": self.model, "base_url": scrub_url(self.base_url), "api_key_env": self.api_key_env,
               "temperature": None if _reasoning_model(self.model) else self.temperature,
               "max_tokens": self.max_tokens}
        if self.extra:
            out["extra"] = dict(self.extra)
        if self.samples():
            out["cache_salt"] = self.cache_salt  # in the manifest, so a resume can reuse this run's draws
        return out

    def _body(self, messages: list[dict], max_tokens: int, response_format: dict | None) -> dict:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "n": 1}
        if _reasoning_model(self.model):
            body["max_completion_tokens"] = max_tokens
        else:
            body["temperature"] = self.temperature
            body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format
        for key, value in self.extra.items():
            body.setdefault(key, value)
        return body

    def complete(self, messages: str | list[dict], *, max_tokens: int | None = None,
                 response_format: dict | None = None) -> SimpleNamespace:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        body = self._body(list(messages), max_tokens or self.max_tokens, response_format)
        key_parts = [self.base_url, json.dumps(body, sort_keys=True, ensure_ascii=False)]
        if self.samples():
            key_parts.append(self.cache_salt)
        key = hashlib.sha256("\n".join(key_parts).encode("utf-8")).hexdigest()
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                self.meter.add(self.model, 0, 0, cached=True)
                return SimpleNamespace(text=hit["text"], model=hit.get("model"), usage=hit.get("usage", {}), cached=True)
        data = self._post(body)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response shape from {self.model}: keys={sorted(data)}") from None
        if text is None:
            raise LLMError(f"{self.model} returned no text (finish_reason={choice.get('finish_reason')})")
        usage = data.get("usage")
        if not isinstance(usage, dict) or "prompt_tokens" not in usage:
            raise LLMError(f"{self.model} response has no usage block; refusing to record unmetered output")
        self.meter.add(self.model, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), cached=False,
                       reported_usd=_reported_cost(usage))
        result = {"text": text, "model": data.get("model"), "usage": usage}
        stored = self.cache.put(key, result) if self.cache is not None else result
        # A racing call stored its response first: answer with that one, so a rerun from cache agrees.
        return SimpleNamespace(text=stored["text"], model=stored.get("model"), usage=stored.get("usage", {}),
                               cached=stored != result)

    def invoke(self, prompt: str | list[dict]) -> SimpleNamespace:
        """LangChain-style call, used by the BEAM evaluation functions: returns an object with .content."""
        return SimpleNamespace(content=self.complete(prompt).text)

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        payload = json.dumps(body).encode("utf-8")
        last = "no attempt made"
        for attempt in range(self.retries):
            req = urllib.request.Request(self.base_url + "/chat/completions", data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                detail = scrub(exc.read()[:400].decode("utf-8", "replace"))
                last = f"HTTP {exc.code}: {detail}"
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    raise LLMError(f"{self.model}: {last}") from None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                last = scrub(f"{type(exc).__name__}: {exc}")
                delay = None
            time.sleep(delay if delay is not None else min(60.0, 2 ** attempt + random.random()))
        raise LLMError(f"{self.model}: gave up after {self.retries} attempts ({last})")
