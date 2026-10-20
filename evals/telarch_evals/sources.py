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
MAB_COMMIT = "538026089d1a8a8eff05121d0db89b388f360eba"
MAB_DATA = "7ea066982b140a19337e17e60d45d4076e042faf"
# nltk_data commit of the punkt_tab sentence tokenizer that MemoryAgentBench's chunker uses.
NLTK_DATA_COMMIT = "4f15a3d89eefe9748ec1c05be495d91289197155"

_GH = "https://raw.githubusercontent.com"
_HF_LME = f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{LONGMEMEVAL_DATA}"
_HF_MAB = f"https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/{MAB_DATA}"
_GH_MAB = f"{_GH}/HUST-AI-HYZ/MemoryAgentBench/{MAB_COMMIT}"


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
    "memoryagentbench/Accurate_Retrieval.parquet": Source(
        f"{_HF_MAB}/data/Accurate_Retrieval-00000-of-00001.parquet", "MIT",
        sha256="56c3cd80fb6731a3e53cd1a6be3148f54df60ff2d290ee50e28f8acebf9655c1", size=20024386),
    "memoryagentbench/Test_Time_Learning.parquet": Source(
        f"{_HF_MAB}/data/Test_Time_Learning-00000-of-00001.parquet", "MIT",
        sha256="5338753be48f925d03318eed66117286e3489025fabe050a547bd086cd7d79c0", size=3947476),
    "memoryagentbench/Long_Range_Understanding.parquet": Source(
        f"{_HF_MAB}/data/Long_Range_Understanding-00000-of-00001.parquet", "MIT",
        sha256="5ab175461954db67770d4a4cb69e569b513ebb96aceb9ee79b57f67488bcd539", size=49342452),
    "memoryagentbench/Conflict_Resolution.parquet": Source(
        f"{_HF_MAB}/data/Conflict_Resolution-00000-of-00001.parquet", "MIT",
        sha256="24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45", size=1491588),
    "memoryagentbench/entity2id.json": Source(
        f"{_HF_MAB}/entity2id.json", "MIT",
        sha256="63353aca481bc9558b502f91cb98f6fa26438796fdd7e0bc06b5a1532126e8b5", size=1758081),
    "memoryagentbench/templates.py": Source(
        f"{_GH_MAB}/utils/templates.py", "MIT",
        sha256="148c40d48d19f155ae845482c4417ba59cfa7ae4e194019509e023bd3a8755dd", size=11961),
    "memoryagentbench/eval_other_utils.py": Source(
        f"{_GH_MAB}/utils/eval_other_utils.py", "MIT",
        sha256="d77976be409298970614d477a9d8003850caddb0510e56a7e821a037d98493a2", size=23895),
    "memoryagentbench/longmem_qa_evaluate.py": Source(
        f"{_GH_MAB}/llm_based_eval/longmem_qa_evaluate.py", "MIT",
        sha256="ceca59bddea4203a78b417be635e63fe3f685bd957e3df12150978e21972c540", size=11898),
    "memoryagentbench/summarization_evaluate.py": Source(
        f"{_GH_MAB}/llm_based_eval/summarization_evaluate.py", "MIT",
        sha256="6951d19644cdb1972bcf6cdb272f4757aa995ae07099bf74012925158c376dd4", size=63584),
    "nltk/punkt_tab.zip": Source(
        f"{_GH}/nltk/nltk_data/{NLTK_DATA_COMMIT}/packages/tokenizers/punkt_tab.zip", "Apache-2.0",
        sha256="e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106", size=4319076),
}

MAB_SPLITS = ("Accurate_Retrieval", "Test_Time_Learning", "Long_Range_Understanding", "Conflict_Resolution")
MAB_CODE = ("memoryagentbench/templates.py", "memoryagentbench/eval_other_utils.py",
            "memoryagentbench/longmem_qa_evaluate.py", "memoryagentbench/summarization_evaluate.py",
            "memoryagentbench/entity2id.json", "nltk/punkt_tab.zip")

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
