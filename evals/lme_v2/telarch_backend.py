"""Telarch (Kibisis + Harpe) as a LongMemEval-V2 memory backend, text only.

Each state of a trajectory becomes passages: one with the trajectory's goal and outcome, the URL, the agent's
thought and its action, then the page text (accessibility tree) in chunks of about 512 tokens, each labelled with
its trajectory and step. Retrieval and packing are the telarch-evals Telarch arm's (`TelarchMemory`), with the
settings passed in `memory_params` (the run wrapper takes them from reports/round2-config.json). Passages are
ingested while the harness inserts trajectories, so a query only searches and packs. Screenshots are not used.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from memory_modules.memory import Memory, MemoryContextItem, register_memory
from telarch_evals.memory import TelarchMemory, Unit

CHUNK_CHARS = 2000  # about 512 o200k tokens of accessibility-tree text
TELARCH_PARAMS = ("budget", "candidates", "per_term", "policy", "encrypt", "counter", "planner", "fill_floor",
                  "recency", "mmr", "bridge")


def _chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Page text in pieces of at most `size` characters, split at line breaks where possible."""
    out: list[str] = []
    buf: list[str] = []
    used = 0
    for line in text.splitlines():
        while len(line) > size:  # a single long line: cut it
            if buf:
                out.append("\n".join(buf))
                buf, used = [], 0
            out.append(line[:size])
            line = line[size:]
        if used + len(line) + 1 > size and buf:
            out.append("\n".join(buf))
            buf, used = [], 0
        buf.append(line)
        used += len(line) + 1
    if buf and "".join(buf).strip():
        out.append("\n".join(buf))
    return [c for c in out if c.strip()]


def _states(trajectory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Goal and states of a trajectory in either published schema (`states`, or `content` with `metadata`)."""
    goal = trajectory.get("goal")
    if not isinstance(goal, str):
        raw = (trajectory.get("metadata") or {}).get("original_goal")
        goal = raw if isinstance(raw, str) else str(raw or "")
    states = trajectory.get("states") or trajectory.get("content") or []
    out = []
    for i, state in enumerate(states):
        text = state.get("accessibility_tree", state.get("text"))
        if text is None:
            text = (state.get("observation") or {}).get("text", "")
        step = state.get("step") if isinstance(state.get("step"), int) else state.get("state_index", i)
        out.append({"step": step, "url": state.get("url") or "", "action": state.get("action") or "",
                    "thought": state.get("thought", state.get("thoughts")) or "", "text": text or ""})
    return goal, out


def trajectory_units(trajectory: dict[str, Any], index: int) -> list[Unit]:
    tid = str(trajectory["id"])
    goal, states = _states(trajectory)
    outcome = trajectory.get("outcome")
    units: list[Unit] = []
    for state in states:
        head = f"[trajectory {tid} step {state['step']}] goal: {goal}"
        if outcome:
            head += f" (outcome: {outcome})"
        head += f"\nurl: {state['url']}"
        core = head
        if state["thought"]:
            core += f"\nthought: {state['thought']}"
        if state["action"]:
            core += f"\naction: {state['action']}"
        parts = [core] + [f"{head}\npage (part {j}):\n{chunk}" for j, chunk in enumerate(_chunks(state["text"]), 1)]
        for j, text in enumerate(parts):
            units.append(Unit(uid=f"{tid}#{state['step']}#{j}", session=tid, order=(index, state["step"], j),
                              date="", text=text, rendered=text))
    return units


@register_memory
class TelarchLmeV2Memory(Memory):
    """Kibisis stores every passage (encrypted by default); Harpe packs the any-word BM25 matches."""

    memory_type = "telarch"

    def __init__(self, memory_params: dict[str, object]) -> None:
        super().__init__(memory_params)
        options = {k: memory_params[k] for k in TELARCH_PARAMS if k in memory_params}
        workspace = memory_params.get("workspace_dir")
        root = Path(str(workspace)) if workspace else Path(tempfile.mkdtemp(prefix="telarch-lmev2-"))
        self._memory = TelarchMemory(root / "telarch-vaults", **options)
        self._history = "haystack"
        self._opened = False
        self._inserted = 0
        self._digest = hashlib.sha256()
        self.last_stats: dict[str, Any] = {}

    def insert(self, trajectory: dict[str, object]) -> None:
        units = trajectory_units(trajectory, self._inserted)  # type: ignore[arg-type]
        self._inserted += 1
        self._digest.update(str(trajectory.get("id")).encode("utf-8") + b"\n")
        if not self._opened:
            self._memory.open(self._history, units)
            self._opened = True
        else:
            self._memory.add(self._history, units)

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        if not self._opened:
            return []
        recall = self._memory.recall(self._history, query)
        self.last_stats = recall.stats
        return [{"type": "text", "value": unit.rendered} for unit in recall.units]

    def post_query_hook(self, *, query: str, query_image: str | None,
                        memory_context: list[MemoryContextItem]) -> dict[str, object] | None:
        keep = ("selected", "candidates", "searches", "search_hits", "context_tokens", "context_tokens_o200k",
                "fill", "widened", "search_ms", "rank_ms", "harpe_ms")
        return {"telarch": {k: self.last_stats.get(k) for k in keep},
                "haystack_trajectories": self._inserted, "haystack_ids_sha256": self._digest.hexdigest()}
