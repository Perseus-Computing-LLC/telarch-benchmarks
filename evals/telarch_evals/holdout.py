"""Sealed holdout: a fixed dev/holdout partition of each benchmark and a ledger receipt for every holdout read.

The partition is a seeded hash over a benchmark's independent groups: LongMemEval questions (each has its own
history; ranked inside its question type, so the type mix is kept), LoCoMo conversations and BEAM conversations
(their questions share one history, so the conversation is what gets held out). Inside each stratum the groups of
lowest hash are the holdout, `HOLDOUT_BP` of them, rounded half up.

`--partition dev` runs only development groups. `--partition holdout` runs only holdout groups and needs
`--holdout-prereg FILE`: a JSON file with a `prereg` block (`primary_metric`, `threshold`) and a `holdout_digest` equal to
this partition's. Every new run whose selection touches a holdout group, with or without `--partition`, appends
`eval.holdout.read` to the run ledger, so the number of times the holdout was read is a count of ledger events.

The partition was defined on 2026-11-17. Every full run before that date read the groups that are now holdout,
and the ledger has no receipt for those reads; the seal holds from that date on.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PARTITION_VERSION = 1
DEFAULT_SEED = 20261117
HOLDOUT_BP = 3000
PARTITIONS = ("dev", "holdout")
EVENT = "eval.holdout.read"


def _hash(seed: int, benchmark: str, group: str) -> str:
    return hashlib.sha256(f"telarch-evals/partition/v{PARTITION_VERSION}/{seed}/{benchmark}/{group}".encode()).hexdigest()


@dataclass(frozen=True)
class Partition:
    benchmark: str            # "longmemeval/s", "locomo", "beam/100K"
    seed: int
    holdout_bp: int
    groups: frozenset[str]
    holdout: frozenset[str]

    @property
    def digest(self) -> str:
        """SHA-256 of the sorted holdout group ids, one per line."""
        return hashlib.sha256("\n".join(sorted(self.holdout)).encode()).hexdigest()

    def select(self, which: str | None) -> frozenset[str]:
        if which is None:
            return self.groups
        if which not in PARTITIONS:
            raise ValueError(f"unknown partition {which!r}")
        return self.holdout if which == "holdout" else self.groups - self.holdout

    def describe(self) -> dict[str, Any]:
        return {"benchmark": self.benchmark, "version": PARTITION_VERSION, "seed": self.seed,
                "holdout_bp": self.holdout_bp, "groups": len(self.groups), "holdout_groups": len(self.holdout),
                "holdout_digest": self.digest}


def partition(benchmark: str, groups: Mapping[str, str] | Iterable[str], seed: int = DEFAULT_SEED,
              holdout_bp: int = HOLDOUT_BP) -> Partition:
    """`groups` maps each group id to its stratum (an iterable of ids is one stratum). Always pass the benchmark's
    whole group set, not a run's selection: membership is a rank inside the stratum."""
    if not 0 < holdout_bp < 10000:
        raise ValueError("holdout_bp must be between 1 and 9999")
    strata: dict[str, list[str]] = defaultdict(list)
    if isinstance(groups, Mapping):
        for group, stratum in groups.items():
            strata[str(stratum)].append(str(group))
    else:
        strata[""] = [str(g) for g in groups]
    held: set[str] = set()
    total: set[str] = set()
    for members in strata.values():
        if len(set(members)) != len(members):
            raise ValueError(f"{benchmark}: duplicate group ids")
        total.update(members)
        k = (len(members) * holdout_bp + 5000) // 10000
        held.update(sorted(members, key=lambda g: (_hash(seed, benchmark, g), g))[:k])
    return Partition(benchmark, seed, holdout_bp, frozenset(total), frozenset(held))


def check_prereg(path: Path, part: Partition) -> str:
    """The pre-registration a holdout run names: returns its SHA-256, or refuses the run."""
    from .runlog import sha256_file

    try:
        doc = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--holdout-prereg {path}: not a readable JSON file ({exc})") from None
    block = doc.get("prereg") if isinstance(doc, dict) else None
    missing = [k for k in ("primary_metric", "threshold")
               if not (isinstance(block, dict) and isinstance(block.get(k), str) and block[k].strip())]
    if missing:
        raise SystemExit(f"--holdout-prereg {path}: the prereg block lacks {', '.join(missing)}")
    if doc.get("holdout_digest") != part.digest:
        raise SystemExit(f"--holdout-prereg {path}: holdout_digest is not this partition's ({part.digest}); the "
                         f"pre-registration was written for another holdout")
    return sha256_file(Path(path))


@dataclass(frozen=True)
class Read:
    """What a run will read: resolved before the run directory exists, recorded once it does."""
    part: Partition
    which: str | None
    selected: frozenset[str]          # the groups this run may touch
    prereg_sha256: str | None
    prereg_name: str | None

    def config(self) -> dict[str, Any]:
        """The manifest entry of a partitioned run (absent on an unpartitioned one, so earlier runs still resume)."""
        return {"which": self.which, **self.part.describe(),
                **({"prereg_sha256": self.prereg_sha256} if self.prereg_sha256 else {})}

    def sources(self) -> list[dict]:
        return [{"name": "prereg", "file": self.prereg_name, "sha256": self.prereg_sha256}] if self.prereg_sha256 else []


def plan(part: Partition, which: str | None, prereg: Path | None) -> Read:
    if which == "holdout" and prereg is None:
        raise SystemExit("--partition holdout needs --holdout-prereg FILE: a holdout read is made under a pre-registration")
    if prereg is not None and which != "holdout":
        raise SystemExit("--holdout-prereg goes with --partition holdout")
    sha = check_prereg(prereg, part) if prereg is not None else None
    return Read(part, which, part.select(which), sha, Path(prereg).name if prereg is not None else None)


def record_read(log: Any, read: Read, touched: Iterable[str], questions: int, resumed: bool = False) -> dict | None:
    """Append the receipt to the run's ledger. Nothing is written when the run touches no holdout group, or on a
    resume (the read was recorded when the run started)."""
    from .runlog import ACTOR, _ledger

    hit = sorted(set(map(str, touched)).intersection(read.part.holdout))
    if not hit or resumed:
        return None
    payload = {
        "run_id": log.dir.name, "benchmark": log.benchmark, "partition_benchmark": read.part.benchmark,
        "partition_version": PARTITION_VERSION, "seed": read.part.seed, "holdout_bp": read.part.holdout_bp,
        "holdout_digest": read.part.digest, "holdout_groups_total": len(read.part.holdout),
        "holdout_groups_read": len(hit),
        "holdout_groups_read_digest": hashlib.sha256("\n".join(hit).encode()).hexdigest(),
        "questions_selected": int(questions), "partition": read.which or "unpartitioned",
        "prereg_sha256": read.prereg_sha256 or "none", "prereg_file": read.prereg_name or "none",
    }
    _ledger(log.root).append(EVENT, ACTOR, payload)
    return payload


def reads(runs_root: Path) -> list[dict[str, Any]]:
    """Every holdout read the ledger under `runs_root` records, oldest first."""
    from .runlog import _ledger

    if not (Path(runs_root) / "ledger.jsonl").exists():
        return []
    return [{"sequence": e.sequence, **e.payload} for e in _ledger(Path(runs_root)).iter_events()
            if e.event_type == EVENT]
