"""Benign-replay gate (B-R3) and density-matched placebo counterfactual (B-R4) for TCI behaviour rules.

The TCI engine injects the advice lines of its live invariants into the agent's prompt. This module asks,
with a fixed benign task set and pre-registered margins, whether that block costs benign performance and
whether its content does anything a same-size neutral block would not.

Arms (all paired on the same seeded tasks, same model, same token budget):

- ``control``  no block at all
- ``rules``    the current rule block (live invariants only: active / regressed / graduated)
- ``placebo``  a neutral block density-matched to the rule block by token count
- ``prior``    the rule block of an earlier invariants snapshot (the "when rules change" comparison)

Tasks are benign: short tool-use situations with one defensible next call, answerable with no rules. The
model replies with JSON ``{"tool": ..., "args": {...}}`` and a per-family predicate scores it. Scoring is
mechanical; no judge.

Pre-registration: ``gate_config`` gathers everything that decides the verdict; ``preregister`` writes it
with a digest; ``check_prereg`` refuses to launch anything that no longer matches. Verdicts use paired
counts and the pre-registered margin in percentage points.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

BENCHMARK = "rule_gate"
LIVE = ("active", "regressed", "graduated")
ARMS = ("control", "rules", "placebo", "prior")
_TASK_INDEX: dict[str, dict] = {}

_SYSTEM_PREFIX = (
    "You are an engineering agent choosing the next tool call. Reply with one JSON object only: "
    '{"tool": "<name>", "args": {...}}. No prose, no code fences.\n'
    "Available tools and their args:\n"
    "- read_file(path)\n"
    "- search_files(pattern, path, target)  # pattern is a regex for target='content', a glob for target='files'\n"
    "- patch(path, old_string, new_string)  # old_string must match the file exactly as it is now\n"
    "- write_file(path, content)\n"
    "- terminal(command, background)  # background: true keeps the process running after the call returns\n"
    "- memory(operations)  # a list of {action: add|remove|patch, content?, old_text?} applied in one call\n"
)
PROMPT_VERSION = 2   # v1 sent the control arm an empty system message (no format instruction, no tool list)
TASKS_VERSION = 2    # v1 patch_after_edit accepted only the patch call (not neutral to the "re-read after edit" rule)
SCORING_VERSION = 2  # v1 rejected JSON missing up to three closing brackets (a right answer with a dropped brace scored wrong)

_NEUTRAL = [
    "The lighthouse keeper logs the tide twice a day and files the sheet in a drawer.",
    "A harbour ferry runs every forty minutes in winter and every twenty in summer.",
    "The allotment society meets on the first Tuesday and shares seed catalogues.",
    "Beech leaves turn copper in October and hold on through most of the winter.",
    "The town band rehearses in the old library hall on Thursday evenings.",
    "Marmalade sets better when the peel is cut thin and boiled hard for a minute.",
    "The night train slows at the viaduct and the carriages sway for half a mile.",
    "A pottery kiln reaches temperature slowly and cools for a full day before opening.",
    "The canal towpath is gravel on the east bank and grass on the west.",
    "Chess club pairs players by rating and swaps colours every round.",
]


def approx_tokens(text: str) -> int:
    """Stable word-piece estimate: words plus punctuation runs. Good enough to match densities."""
    return len(re.findall(r"\w+|[^\w\s]", text))


# ---------------------------------------------------------------- tasks

def _families():
    dirs = ["src", "lib", "app", "pkg", "tools", "ops"]
    names = ["config", "engine", "ledger", "vault", "router", "scheduler", "parser", "metrics"]
    exts = ["py", "rs", "toml", "md", "json"]
    pats = ["get(", "foo[0]", "a+b", "x*y", "(a|b)", "list[int]", "{k}", "v?"]

    def read_path(r):
        d, n, e = r.choice(dirs), r.choice(names), r.choice(exts)
        wrong = f"{d}/{n}.{e}"
        right = f"{d}/{r.choice(['core', 'internal', 'impl'])}/{n}.{e}"
        prompt = (f"read_file('{wrong}') returned file-not-found. A listing of the project files shows exactly one file "
                  f"named {n}.{e}: '{right}'. You need its contents. Next call?")
        return prompt, {"tool": "read_file", "args": {"path": right}}, {"kind": "arg_equals", "arg": "path", "value": right}

    def regex_escape(r):
        lit = r.choice(pats)
        prompt = (f"Find every line in the repository containing the literal text {lit!r} (the characters exactly as "
                  f"written) using search_files, which takes a regex. Next call?")
        esc = re.escape(lit)
        return prompt, {"tool": "search_files", "args": {"pattern": esc}}, {"kind": "regex_matches_literal", "arg": "pattern", "value": lit}

    def patch_after_edit(r):
        n = r.choice(names)
        old = f"timeout = {r.randint(10, 90)}"
        new = f"timeout = {r.randint(100, 900)}"
        prompt = (f"You just edited {n}.py with patch (old_string '{old}' -> '{new}') and it succeeded. Now you must "
                  f"change the line that currently reads '{new}' to 'timeout = 1200'. Next call?")
        # neutral to the rule set: patching with the exact current text and re-reading the file first are both
        # defensible next calls (TASKS_VERSION 2; v1 accepted only the patch, which made the task a test of
        # whether the model obeys the "re-read after an edit" rule rather than a benign task)
        return prompt, {"tool": "patch", "args": {"path": f"{n}.py", "old_string": new, "new_string": "timeout = 1200"}}, \
            {"kind": "any_of", "value": [{"kind": "arg_equals", "arg": "old_string", "value": new, "tool": "patch"},
                                         {"kind": "arg_equals", "arg": "path", "value": f"{n}.py", "tool": "read_file"}]}

    def background_process(r):
        port = r.randint(3000, 9000)
        cmd = f"python -m http.server {port}"
        prompt = (f"Start a development server with '{cmd}' using the terminal tool. It must keep running after the "
                  f"call returns so you can probe it in a later call. Next call?")
        return prompt, {"tool": "terminal", "args": {"command": cmd, "background": True}}, {"kind": "arg_true", "arg": "background"}

    def write_after_read(r):
        n = r.choice(names)
        path = f"{r.choice(dirs)}/{n}.md"
        prompt = (f"'{path}' exists with content you have not read in this task. You want to replace its first heading only. "
                  f"Which call first?")
        return prompt, {"tool": "read_file", "args": {"path": path}}, {"kind": "tool_in", "value": ["read_file"]}

    def memory_budget(r):
        prompt = ("A memory add was rejected: the store is at its character limit. One stale entry can be removed "
                  "and the new fact added. Do it in one call. Next call?")
        ref = {"tool": "memory", "args": {"operations": [{"action": "remove"}, {"action": "add"}]}}
        return prompt, ref, {"kind": "ops_contains", "arg": "operations", "value": ["remove", "add"]}

    def path_exists_first(r):
        d = f"{r.choice(dirs)}/{r.choice(['legacy', 'archive', 'old'])}"
        prompt = (f"search_files over '{d}' failed: the directory does not exist. The parent '{d.split('/')[0]}' does. "
                  f"You want to find where the old files went. Next call?")
        return prompt, {"tool": "search_files", "args": {"path": d.split('/')[0], "target": "files", "pattern": "*"}}, \
            {"kind": "arg_equals", "arg": "path", "value": d.split('/')[0]}

    return {"read_path": read_path, "regex_escape": regex_escape, "patch_after_edit": patch_after_edit,
            "background_process": background_process, "write_after_read": write_after_read,
            "memory_budget": memory_budget, "path_exists_first": path_exists_first}


def make_tasks(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    fams = _families()
    order = sorted(fams)
    tasks = []
    for i in range(n):
        fam = order[i % len(order)]
        prompt, ref, check = fams[fam](rng)
        tid = hashlib.sha256(f"{seed}:{i}:{fam}:{prompt}:v{TASKS_VERSION}".encode()).hexdigest()[:12]
        t = {"id": tid, "family": fam, "prompt": prompt, "reference": ref, "check": check}
        tasks.append(t)
        _TASK_INDEX[tid] = t
    return tasks


def task_by_id(tid: str) -> dict:
    return _TASK_INDEX[tid]


def _parse(text: str) -> dict | None:
    text = text.strip()
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]
    end = body.rfind("}")
    if end >= 0:
        body = body[:end + 1]
    obj = _loads(body)
    if obj is None:
        # scoring v2: repair up to three missing closing brackets, in the order the open ones need
        stack = []
        in_str, esc = False, False
        for ch in body:
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        if 0 < len(stack) <= 3:
            obj = _loads(body + "".join(reversed(stack)))
    return obj if isinstance(obj, dict) and isinstance(obj.get("tool"), str) else None


def _loads(body: str) -> dict | None:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def check_answer(task: dict, text: str) -> bool:
    obj = _parse(text)
    if obj is None:
        return False
    return _check(obj, task["check"], task["reference"]["tool"])


def _check(obj: dict, c: dict, ref_tool: str) -> bool:
    args: dict = obj["args"] if isinstance(obj.get("args"), dict) else {}
    k = c["kind"]
    if k == "any_of":
        return any(_check(obj, alt, alt.get("tool", ref_tool)) for alt in c["value"])
    ref_tool = c.get("tool", ref_tool)
    if k == "tool_in":
        return obj["tool"] in c["value"]
    if obj["tool"] != ref_tool:
        return False
    if k == "arg_equals":
        return str(args.get(c["arg"], "")).strip() == c["value"]
    if k == "arg_true":
        return args.get(c["arg"]) is True
    if k == "regex_matches_literal":
        pat = args.get(c["arg"])
        if not isinstance(pat, str):
            return False
        try:
            rx = re.compile(pat)
        except re.error:
            return False
        lit = c["value"]
        return rx.search(f"xx {lit} yy") is not None and rx.search("xx get yy foo a b") is None
    if k == "ops_contains":
        ops = args.get(c["arg"])
        if not isinstance(ops, list):
            return False
        actions = [o.get("action") for o in ops if isinstance(o, dict)]
        return all(a in actions for a in c["value"])
    return False


# ---------------------------------------------------------------- blocks

def rule_block(invariants: dict) -> str:
    inv = invariants.get("invariants", invariants)
    lines = sorted(str(v.get("advice") or "").strip() for v in inv.values()
                   if isinstance(v, dict) and v.get("status") in LIVE and v.get("advice"))
    return "\n".join(f"- {ln}" for ln in lines if ln)


def placebo_block(block: str, seed: int) -> str:
    """Neutral sentences, repeated and trimmed until the token count matches the rule block."""
    target = approx_tokens(block)
    if target == 0:
        return ""
    rng = random.Random(seed)
    pool = _NEUTRAL[:]
    rng.shuffle(pool)
    lines, i = [], 0
    while approx_tokens("\n".join(lines)) < target:
        lines.append(f"- {pool[i % len(pool)]}")
        i += 1
    text = "\n".join(lines)
    while approx_tokens(text) > target and len(text) > 1:
        text = text[:-1]
    return text


def _messages(arm_block: str, task: dict) -> list[dict]:
    """Every arm gets the same prefix (format + tool list); only the guidance block differs, and control has none."""
    system = _SYSTEM_PREFIX + ("\nStanding guidance:\n" + arm_block + "\n" if arm_block else "")
    user = f"TASK-ID: {task['id']}\n{task['prompt']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------- pre-registration

class PreregMismatch(Exception):
    pass


def gate_config(*, invariants: dict, n_tasks: int, seed: int, model: str, max_tokens: int, margin_pp: float,
                arms: tuple[str, ...] = ("control", "rules", "placebo"), prior_invariants: dict | None = None,
                temperature: float = 0.0) -> dict:
    block = rule_block(invariants)
    tasks = make_tasks(n_tasks, seed)
    cfg: dict[str, Any] = {
        "benchmark": BENCHMARK, "system": "rules", "n_tasks": n_tasks, "seed": seed, "model": model,
        "max_tokens": max_tokens, "temperature": temperature, "margin_pp": margin_pp, "arms": list(arms),
        "blocks": {"rules": block, "placebo": placebo_block(block, seed), "control": ""},
        "tasks_sha256": hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest(),
        "prompt_version": PROMPT_VERSION, "scoring_version": SCORING_VERSION, "tasks_version": TASKS_VERSION,
        "prompt_sha256": hashlib.sha256(_SYSTEM_PREFIX.encode()).hexdigest(),
    }
    if "prior" in arms:
        if prior_invariants is None:
            raise ValueError("the prior arm needs --prior-invariants")
        cfg["blocks"]["prior"] = rule_block(prior_invariants)
    return cfg


def prereg_digest(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def preregister(cfg: dict, path: Path) -> dict:
    m = cfg["margin_pp"]
    pre = {
        "benchmark": BENCHMARK, "digest": prereg_digest(cfg), "config": cfg,
        "rule_block_sha256": hashlib.sha256(cfg["blocks"]["rules"].encode()).hexdigest(),
        "tasks_sha256": cfg["tasks_sha256"],
        "primary_metric": "benign solve rate per arm on the same seeded tasks; paired per task",
        "decision_rule": {
            "b_r3": f"gate fails when solve_rate(rules) - solve_rate(control) < -{m} pp; passes otherwise. "
                    f"If a prior arm is present it is reported alongside, same margin, rules vs prior.",
            "b_r4": f"content_effect when solve_rate(rules) - solve_rate(placebo) >= +{m} pp; harm when <= -{m} pp; "
                    f"null otherwise. placebo is density-matched to the rule block by token count.",
        },
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pre, indent=1, sort_keys=True) + "\n", "utf-8")
    return pre


def check_prereg(cfg: dict, path: Path) -> str:
    pre = json.loads(Path(path).read_text("utf-8"))
    want = prereg_digest(cfg)
    if pre.get("digest") != want:
        old = pre.get("config", {})
        changed = sorted(k for k in set(cfg) | set(old) if cfg.get(k) != old.get(k))
        raise PreregMismatch(f"pre-registration {path} does not match this launch (changed: {', '.join(changed)})")
    return want


# ---------------------------------------------------------------- running and verdicts

def run_arms(cfg: dict, reader, prereg_path: Path, run=None, meter=None, max_usd: float | None = None) -> list[dict]:
    """Paired: for each task, every arm in order. Rows go to ``run`` (a RunLog) when given."""
    digest = check_prereg(cfg, prereg_path)
    tasks = make_tasks(cfg["n_tasks"], cfg["seed"])
    rows: list[dict] = []
    done = run.done if run is not None else set()
    for t in tasks:
        for arm in cfg["arms"]:
            rid = f"{t['id']}:{arm}"
            if rid in done:
                continue
            if max_usd is not None and meter is not None and meter.usd() >= max_usd:
                return rows
            resp = reader.complete(_messages(cfg["blocks"][arm], t), max_tokens=cfg["max_tokens"])
            text = getattr(resp, "text", "") or ""
            row = {"id": rid, "task_id": t["id"], "family": t["family"], "arm": arm, "solved": check_answer(t, text),
                   "answer": text[:400], "prereg_digest": digest, "cached": bool(getattr(resp, "cached", False)),
                   "status": "ok"}
            rows.append(row)
            if run is not None:
                run.write(row)
    return rows


def _rate(rows: list[dict], arm: str) -> tuple[int, int]:
    r = [x for x in rows if x["arm"] == arm and x.get("status", "ok") == "ok"]
    return sum(1 for x in r if x["solved"]), len(r)


def _paired(rows: list[dict], a: str, b: str) -> dict:
    by: dict[str, dict[str, bool]] = {}
    for x in rows:
        if x.get("status", "ok") == "ok":
            by.setdefault(x["task_id"], {})[x["arm"]] = bool(x["solved"])
    pairs = [(v[a], v[b]) for v in by.values() if a in v and b in v]
    return {"n": len(pairs), f"{a}_worse": sum(1 for p, q in pairs if q and not p),
            f"{a}_better": sum(1 for p, q in pairs if p and not q), "both": sum(1 for p, q in pairs if p and q),
            "neither": sum(1 for p, q in pairs if not p and not q)}


def verdict(rows: list[dict], cfg: dict) -> dict:
    m = float(cfg["margin_pp"])
    arms: dict[str, Any] = {}
    for arm in cfg["arms"]:
        k, n = _rate(rows, arm)
        arms[arm] = {"solved": k, "n": n, "solve_rate": (k / n) if n else None}
    by_family: dict[str, dict] = {}
    for x in rows:
        if x.get("status", "ok") != "ok":
            continue
        f = by_family.setdefault(x["family"], {})
        f.setdefault(x["arm"], [0, 0])
        f[x["arm"]][0] += int(x["solved"])
        f[x["arm"]][1] += 1

    def delta(a, b):
        ra, rb = arms[a]["solve_rate"], arms[b]["solve_rate"]
        return None if ra is None or rb is None else round(100.0 * (ra - rb), 2)

    out: dict[str, Any] = {"benchmark": BENCHMARK, "prereg_digest": prereg_digest(cfg), "margin_pp": m, "arms": arms,
                           "by_family": {f: {a: {"solved": v[0], "n": v[1]} for a, v in d.items()} for f, d in sorted(by_family.items())}}
    d3 = delta("rules", "control")
    out["b_r3"] = {"comparison": "rules - control", "delta_pp": d3,
                   "status": "incomplete" if d3 is None else ("fail" if d3 < -m else "pass"),
                   "paired": _paired(rows, "rules", "control")}
    if "prior" in cfg["arms"]:
        dp = delta("rules", "prior")
        out["b_r3"]["prior"] = {"comparison": "rules - prior", "delta_pp": dp,
                                "status": "incomplete" if dp is None else ("fail" if dp < -m else "pass"),
                                "paired": _paired(rows, "rules", "prior")}
    if "placebo" in cfg["arms"]:
        d4 = delta("rules", "placebo")
        out["b_r4"] = {"comparison": "rules - placebo", "delta_pp": d4,
                       "status": "incomplete" if d4 is None else ("content_effect" if d4 >= m else ("harm" if d4 <= -m else "null")),
                       "paired": _paired(rows, "rules", "placebo")}
    return out
