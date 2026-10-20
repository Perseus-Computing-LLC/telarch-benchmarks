"""Pinned upstream inputs: benchmark datasets and the official evaluation code.

Every file comes from an immutable address (a commit SHA or a dataset revision) and is checked
against the hash recorded here before it is used. None of these files are copied into this
repository; each keeps its own license, listed with its source.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

USER_AGENT = "telarch-evals/0.1 (+https://github.com/Perseus-Computing-LLC/telarch-benchmarks)"

LONGMEMEVAL_CODE = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
LONGMEMEVAL_DATA = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
LOCOMO_COMMIT = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
BEAM_COMMIT = "b2da22eac88bb0874c64665f13457eb99835774a"
# The last commit that still has evaluation/metrics/llm_judge.py (the LoCoMo "J" judge prompt).
MEM0_COMMIT = "b3ede5b7c0ac0e847b03786a603c107ac943b3ee"

_GH = "https://raw.githubusercontent.com"
_HF_LME = f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{LONGMEMEVAL_DATA}"


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    url: str
    license: str
    sha256: str | None = None
    git_blob_sha1: str | None = None
    size: int | None = None


SOURCES: dict[str, Source] = {
    "longmemeval/longmemeval_s_cleaned.json": Source(
        f"{_HF_LME}/longmemeval_s_cleaned.json", "MIT",
        sha256="d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442", size=277383467),
    "longmemeval/longmemeval_m_cleaned.json": Source(
        f"{_HF_LME}/longmemeval_m_cleaned.json", "MIT",
        sha256="9d79e5524794a2e6900a3aa9cb7d9152c5a3e8319c9a87c25494ba1eacee495f", size=2737100077),
    "longmemeval/longmemeval_oracle.json": Source(
        f"{_HF_LME}/longmemeval_oracle.json", "MIT",
        sha256="821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c", size=15388478),
    "longmemeval/evaluate_qa.py": Source(
        f"{_GH}/xiaowu0162/LongMemEval/{LONGMEMEVAL_CODE}/src/evaluation/evaluate_qa.py", "MIT",
        sha256="ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251", size=7436),
    "longmemeval/run_generation.py": Source(
        f"{_GH}/xiaowu0162/LongMemEval/{LONGMEMEVAL_CODE}/src/generation/run_generation.py", "MIT",
        sha256="4f1eb3c69d7ad40f04065b9c0bc86f6582441018fc6ff751d162d66c95baf672", size=21608),
    "locomo/locomo10.json": Source(
        f"{_GH}/snap-research/locomo/{LOCOMO_COMMIT}/data/locomo10.json", "CC BY-NC 4.0",
        sha256="79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4", size=2805274),
    "mem0/llm_judge.py": Source(
        f"{_GH}/mem0ai/mem0/{MEM0_COMMIT}/evaluation/metrics/llm_judge.py", "Apache-2.0",
        sha256="ac2e242f6ea0b817ba1b229ff9e5271f6846b7b8a9aefa8a5a5a81ea58df3b3c", size=4872),
    "beam/compute_metrics.py": Source(
        f"{_GH}/mohammadtavakoli78/BEAM/{BEAM_COMMIT}/src/evaluation/compute_metrics.py", "MIT",
        sha256="e72bf25d36d521d166862063ec94f8e7875328193142d1edddb9b4677d47780d", size=19830),
    "beam/prompts.py": Source(
        f"{_GH}/mohammadtavakoli78/BEAM/{BEAM_COMMIT}/src/prompts.py", "MIT",
        sha256="81fe51ab2bcd7e8ad218b3a9a969a340c38d31f90ac2d93fa78a3bb890ba157b", size=633361),
}

LONGMEMEVAL_SPLITS = {
    "s": "longmemeval/longmemeval_s_cleaned.json",
    "m": "longmemeval/longmemeval_m_cleaned.json",
    "oracle": "longmemeval/longmemeval_oracle.json",
}
BEAM_SIZES = ("100K", "500K", "1M", "10M")


def home() -> Path:
    """Cache and results root: $TELARCH_EVALS_HOME, else ~/.cache/telarch-evals."""
    env = os.environ.get("TELARCH_EVALS_HOME")
    return Path(env) if env else Path.home() / ".cache" / "telarch-evals"


@lru_cache(maxsize=1)
def beam_manifest() -> dict:
    """Git blob SHA-1 and size of every BEAM chat and question file at BEAM_COMMIT."""
    text = resources.files("telarch_evals").joinpath("data/beam_manifest.json").read_text("utf-8")
    manifest = json.loads(text)
    if manifest.get("commit") != BEAM_COMMIT:
        raise SourceError("beam_manifest.json was generated for a different BEAM commit")
    return manifest


def beam_conversations(size: str) -> list[str]:
    if size not in BEAM_SIZES:
        raise SourceError(f"unknown BEAM size {size!r}; choose from {', '.join(BEAM_SIZES)}")
    ids = {path.split("/")[1] for path in beam_manifest()["files"] if path.startswith(size + "/")}
    return sorted(ids, key=lambda x: (len(x), x))


def get_source(key: str) -> Source:
    if key in SOURCES:
        return SOURCES[key]
    if key.startswith("beam/chats/"):
        rel = key[len("beam/chats/"):]
        try:
            sha1, size = beam_manifest()["files"][rel]
        except KeyError:
            raise SourceError(f"{rel} is not in the pinned BEAM manifest") from None
        return Source(f"{_GH}/mohammadtavakoli78/BEAM/{BEAM_COMMIT}/chats/{rel}", "MIT",
                      git_blob_sha1=sha1, size=size)
    raise SourceError(f"unknown source {key!r}")


def _matches(path: Path, src: Source) -> bool:
    size = path.stat().st_size
    if src.size is not None and size != src.size:
        return False
    if src.sha256:
        digest = hashlib.sha256()
    else:
        digest = hashlib.sha1()
        digest.update(b"blob %d\0" % size)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest() == (src.sha256 or src.git_blob_sha1)


def _download(url: str, dest: Path, size: int | None, quiet: bool) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        done, next_report = 0, 0.1
        for block in iter(lambda: resp.read(1 << 20), b""):
            out.write(block)
            done += len(block)
            if not quiet and size and size > 50_000_000 and done / size >= next_report:
                print(f"  {done / 1e6:,.0f} of {size / 1e6:,.0f} MB", file=sys.stderr)
                next_report += 0.1


def fetch(key: str, *, quiet: bool = False) -> Path:
    """Return the cached path of a pinned source, downloading and hash-checking it if needed."""
    src = get_source(key)
    dest = home() / "sources" / key
    if dest.exists():
        if _matches(dest, src):
            return dest
        if not quiet:
            print(f"{key}: cached copy does not match the pinned hash; downloading again", file=sys.stderr)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.part-{os.getpid()}")
    if not quiet:
        print(f"fetching {key}", file=sys.stderr)
    for attempt in range(4):
        try:
            _download(src.url, tmp, src.size, quiet)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 3:
                tmp.unlink(missing_ok=True)
                raise SourceError(f"could not download {key}: {exc}") from exc
            time.sleep(2 ** attempt)
    if not _matches(tmp, src):
        tmp.unlink(missing_ok=True)
        raise SourceError(f"{key}: downloaded file does not match the pinned hash")
    os.replace(tmp, dest)
    return dest


def import_local(key: str, path: str | Path) -> Path:
    """Seed the cache from a local copy, which must match the pinned hash."""
    src = get_source(key)
    path = Path(path)
    if not _matches(path, src):
        raise SourceError(f"{path} does not match the pinned hash of {key}")
    dest = home() / "sources" / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.part-{os.getpid()}")
    shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    return dest


def record(key: str) -> dict:
    """Provenance entry for a run manifest."""
    src = get_source(key)
    entry = {"key": key, "url": src.url, "license": src.license}
    if src.sha256:
        entry["sha256"] = src.sha256
    else:
        entry["git_blob_sha1"] = src.git_blob_sha1 or ""
    return entry
