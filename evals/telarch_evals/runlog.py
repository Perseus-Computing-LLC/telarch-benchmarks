"""Run directories, checkpointed result rows, and the Argus ledger of runs.

A run directory holds manifest.json (what was run, with which inputs), judgments.jsonl (one row per
question, appended and fsynced as each finishes, so a stopped run resumes where it left off) and
summary.json. Each start, resume and completion is appended to an Argus hash-chained ledger
(runs/ledger.jsonl) with the SHA-256 of the files it covers; `telarch-evals verify` checks both.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .llm import Meter, scrub
from .memory import package_versions

ACTOR = "telarch-evals"
REPO_ROOT = Path(__file__).resolve().parents[2]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def repo_commit() -> str:
    try:
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no", "--",
                                "evals"], capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _ledger(root: Path):
    try:  # the PyPI distribution argus-ledger installs the module argus_ledger
        from argus_ledger import AppendOnlyLedger
    except ImportError:  # older source checkouts name it argus
        from argus import AppendOnlyLedger

    return AppendOnlyLedger(str(root / "ledger.jsonl"))


class RunLog:
    def __init__(self, runs_root: Path, benchmark: str, config: dict[str, Any], sources: list[dict],
                 resume: Path | None = None) -> None:
        self.root = Path(runs_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.benchmark = benchmark
        self._lock = threading.Lock()
        if resume:
            self.dir = Path(resume)
            self.manifest = json.loads((self.dir / "manifest.json").read_text("utf-8"))
            if self.manifest.get("benchmark") != benchmark:
                raise SystemExit(f"{self.dir} is a {self.manifest.get('benchmark')} run, not {benchmark}")
            if self.manifest.get("config") != config:
                changed = sorted(k for k in set(config) | set(self.manifest.get("config", {}))
                                 if config.get(k) != self.manifest.get("config", {}).get(k))
                raise SystemExit(f"cannot resume {self.dir.name}: settings differ ({', '.join(changed)})")
            event = "eval.run.resumed"
        else:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
            self.dir = self.root / benchmark / f"{stamp}-{config.get('system', 'run')}-{digest}"
            self.dir.mkdir(parents=True, mode=0o700)
            self.manifest = {
                "tool": "telarch-evals", "version": __version__, "benchmark": benchmark,
                "run_id": self.dir.name, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "repo_commit": repo_commit(), "python": sys.version.split()[0],
                "packages": package_versions(), "sources": sources, "config": config,
            }
            _write_private(self.dir / "manifest.json", json.dumps(self.manifest, indent=2, sort_keys=True) + "\n")
            event = "eval.run.started"
        self.rows_path = self.dir / "judgments.jsonl"
        if not self.rows_path.exists():
            _write_private(self.rows_path, "")
        self.done = {row["id"] for row in self.rows() if row.get("status") == "ok"}
        self._drop_failed_rows()
        _ledger(self.root).append(event, ACTOR, {
            "run_id": self.dir.name, "benchmark": benchmark,
            "manifest_sha256": sha256_file(self.dir / "manifest.json"), "rows_done": len(self.done)})
        self._fh = open(self.rows_path, "a", encoding="utf-8")

    def _drop_failed_rows(self) -> None:
        """On resume, failed rows are retried: keep only completed rows in the file."""
        rows = self.rows()
        kept = [row for row in rows if row.get("status") == "ok"]
        if len(kept) != len(rows):
            _write_private(self.rows_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))

    def rows(self) -> list[dict]:
        if not self.rows_path.exists():
            return []
        out = []
        with open(self.rows_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:  # a torn final line from an interrupted write
                        continue
        return out

    def write(self, row: dict) -> None:
        with self._lock:
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def finish(self, summary: dict, extra_files: dict[str, str] | None = None) -> Path:
        self._fh.close()
        for name, text in (extra_files or {}).items():
            _write_private(self.dir / name, text)
        _write_private(self.dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
        files = {name: sha256_file(self.dir / name)
                 for name in ["manifest.json", "judgments.jsonl", "summary.json", *(extra_files or {})]}
        _ledger(self.root).append("eval.run.completed", ACTOR, {
            "run_id": self.dir.name, "benchmark": self.benchmark, "rows": summary.get("rows"),
            "errors": summary.get("errors"), "complete": summary.get("complete"), "files_sha256": files})
        return self.dir


def execute(run: RunLog, jobs: Iterable[tuple[str, Any]], work: Callable[[str, Any], dict], *, workers: int,
            meter: Meter, max_usd: float | None) -> str | None:
    """Run `work` for each job on a thread pool and checkpoint every row.

    `jobs` is consumed on the calling thread (that is where memory ingestion and recall happen), so
    a memory store is only ever touched from one thread. Returns a stop reason, or None.
    """

    def guarded(job_id: str, payload: Any) -> dict:
        try:
            row = work(job_id, payload)
            row.setdefault("status", "ok")
            return row
        except Exception as exc:  # recorded and retried on resume; never counted as a wrong answer
            return {"id": job_id, "status": "error", "error": scrub(f"{type(exc).__name__}: {exc}")[:500]}

    stop = None
    pending: set[Future] = set()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for job_id, payload in jobs:
            if max_usd is not None and meter.usd() >= max_usd:
                stop = f"estimated spend reached --max-usd {max_usd}"
                break
            pending.add(pool.submit(guarded, job_id, payload))
            if len(pending) >= 2 * max(1, workers):
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    run.write(fut.result())
        for fut in pending:
            run.write(fut.result())
    return stop


def verify(runs_root: Path) -> dict[str, Any]:
    """Check the ledger's hash chain, then the files of every completed run against it."""
    root = Path(runs_root)
    path = root / "ledger.jsonl"
    if not path.exists():
        return {"ledger": str(path), "valid": False, "error": "no ledger"}
    ledger = _ledger(root)
    report = ledger.verify_chain()
    result: dict[str, Any] = {"ledger": str(path), "chain_valid": bool(report.valid),
                              "events": report.total_events, "head_hash": report.head_hash,
                              "error": report.error_detail, "runs": []}
    for event in ledger.iter_events():
        if event.event_type != "eval.run.completed":
            continue
        payload = event.payload
        run_dir = root / payload["benchmark"] / payload["run_id"]
        mismatched = []
        for name, digest in payload.get("files_sha256", {}).items():
            file = run_dir / name
            if not file.exists() or sha256_file(file) != digest:
                mismatched.append(name)
        result["runs"].append({"run_id": payload["run_id"], "benchmark": payload["benchmark"],
                               "files_match": not mismatched, "mismatched": mismatched})
    result["valid"] = result["chain_valid"] and all(r["files_match"] for r in result["runs"])
    return result
