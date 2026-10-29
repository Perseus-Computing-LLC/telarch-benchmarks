"""B-R3 + B-R4 (P4): the benign-replay gate and the equal-budget no-feedback counterfactual for behaviour rules.

The TCI engine injects behaviour rules (the advice lines of live invariants) into the agent's context. Two
questions must be answered with data every time the rule set or its thresholds change:

B-R3  Do the rules cost benign performance? A fixed set of benign tool-use tasks — solvable with no rules at
      all — is solved under ``control`` (no rule block) and ``rules`` (the current block). The gate fails
      when the rules arm is worse than control by more than the pre-registered margin.
B-R4  Does the *content* of the rules matter, or is any same-size block as good? ``placebo`` is a
      density-matched arm: a neutral block of the same token count. Verdict ``content_effect`` when rules
      beat placebo by the margin, ``harm`` when placebo beats rules by the margin, ``null`` otherwise.

Everything that decides the verdict is pre-registered before a model is called: tasks (seeded), arms,
margins, model, token budget, and a digest over all of it. Launch refuses a mismatching pre-registration.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from telarch_evals import rule_gate as rg

INVARIANTS = {"invariants": {
    "read_file.not_found": {"status": "active", "tool": "read_file",
                            "advice": "read_file: confirm exact paths with search_files(target='files') before reading."},
    "terminal.foreground_background": {"status": "graduated", "tool": "terminal",
                                       "advice": "terminal: never end a foreground command with '&'; use background=True."},
    "patch.no_match": {"status": "parked", "tool": "patch", "advice": "patch: should not be injected while parked."},
    "x.candidate": {"status": "candidate", "tool": "x", "advice": "candidate: not injected either."},
}}


def test_tasks_are_seeded_benign_and_self_checking():
    a = rg.make_tasks(n=24, seed=3)
    b = rg.make_tasks(n=24, seed=3)
    assert a == b and len(a) == 24 and len({t["id"] for t in a}) == 24
    assert rg.make_tasks(n=24, seed=4) != a
    fams = {t["family"] for t in a}
    assert fams >= {"read_path", "regex_escape", "patch_after_edit", "background_process", "write_after_read", "memory_budget"}
    for t in a:
        assert set(t) >= {"id", "family", "prompt", "check"}
        assert "rule" not in t["prompt"].lower()   # the task never names the rule it is benign to
        # the reference answer passes its own check; a wrong tool fails it
        assert rg.check_answer(t, json.dumps(t["reference"])) is True
        assert rg.check_answer(t, json.dumps({"tool": "nope", "args": {}})) is False
        assert rg.check_answer(t, "not json at all") is False
        # scoring v2: a dropped closing brace (or three) still scores; a different tool never does
        full = json.dumps(t["reference"])
        assert rg.check_answer(t, full[:-1]) is True and rg.check_answer(t, "```json\n" + full + "\n```") is True
        assert rg.check_answer(t, json.dumps({"tool": "nope", "args": t["reference"]["args"]})[:-1]) is False


def test_patch_after_edit_is_neutral_to_the_reread_rule():
    rg._TASK_INDEX.clear()
    t = next(t for t in rg.make_tasks(n=14, seed=1) if t["family"] == "patch_after_edit")
    path = t["reference"]["args"]["path"]
    assert rg.check_answer(t, json.dumps({"tool": "read_file", "args": {"path": path}})) is True
    assert rg.check_answer(t, json.dumps({"tool": "read_file", "args": {"path": "other.py"}})) is False
    assert rg.check_answer(t, json.dumps({"tool": "patch", "args": {"path": path, "old_string": "timeout = 1", "new_string": "x"}})) is False


def test_rule_block_takes_live_invariants_only_and_placebo_is_density_matched():
    block = rg.rule_block(INVARIANTS)
    assert "confirm exact paths" in block and "background=True" in block
    assert "parked" not in block and "candidate" not in block
    placebo = rg.placebo_block(block, seed=1)
    assert abs(rg.approx_tokens(placebo) - rg.approx_tokens(block)) <= max(2, rg.approx_tokens(block) // 20)
    assert "read_file" not in placebo and "terminal" not in placebo and placebo != block
    assert rg.placebo_block(block, seed=1) == placebo
    assert rg.rule_block({"invariants": {}}) == ""


def test_preregistration_is_digested_and_launch_refuses_a_mismatch(tmp_path):
    cfg = rg.gate_config(invariants=INVARIANTS, n_tasks=12, seed=5, model="fake/reader", max_tokens=200,
                         margin_pp=5.0, arms=("control", "rules", "placebo"))
    pre = rg.preregister(cfg, tmp_path / "prereg.json")
    assert pre["digest"] == rg.prereg_digest(cfg) and (tmp_path / "prereg.json").exists()
    assert pre["decision_rule"]["b_r3"].startswith("gate fails when") and "placebo" in pre["decision_rule"]["b_r4"]
    assert pre["rule_block_sha256"] and pre["tasks_sha256"] and cfg["prompt_sha256"]
    # the prompt shape is part of the registration: a different prefix is a different experiment
    assert rg.prereg_digest(dict(cfg, prompt_sha256="0" * 64)) != pre["digest"]
    cfg2 = dict(cfg, margin_pp=10.0)
    try:
        rg.check_prereg(cfg2, tmp_path / "prereg.json")
    except rg.PreregMismatch as exc:
        assert "margin_pp" in str(exc)
    else:
        raise AssertionError("a changed margin must refuse launch")
    assert rg.check_prereg(cfg, tmp_path / "prereg.json") == pre["digest"]


class _Reader:
    """Deterministic fake: answers correctly unless the arm's block contains a poison word for the family."""
    def __init__(self, fail_when: dict[str, str]):
        self.fail_when, self.calls = fail_when, []
        self.model = "fake/reader"

    def complete(self, messages, *, max_tokens=None, response_format=None):
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        self.calls.append((system, user))
        task_id = user.split("TASK-ID: ")[1].split("\n")[0]
        task = rg.task_by_id(task_id)
        for fam, word in self.fail_when.items():
            if task["family"] == fam and word in system:
                return SimpleNamespace(text=json.dumps({"tool": "nope", "args": {}}), usage={}, cached=False)
        return SimpleNamespace(text=json.dumps(task["reference"]), usage={}, cached=False)


def test_arms_are_run_paired_and_verdicts_follow_the_prereg_margins(tmp_path, monkeypatch):
    rg._TASK_INDEX.clear()
    cfg = rg.gate_config(invariants=INVARIANTS, n_tasks=24, seed=9, model="fake/reader", max_tokens=200,
                         margin_pp=5.0, arms=("control", "rules", "placebo"))
    rg.preregister(cfg, tmp_path / "prereg.json")
    # rules block hurts the regex family badly (the word 'paths' is in the rules block), placebo is harmless
    reader = _Reader({"regex_escape": "paths"})
    rows = rg.run_arms(cfg, reader, tmp_path / "prereg.json")
    assert len(rows) == 24 * 3 and {r["arm"] for r in rows} == {"control", "rules", "placebo"}
    assert all(r["task_id"] and r["arm"] and isinstance(r["solved"], bool) for r in rows)
    # every call was pinned to the pre-registered prompt shapes: control has no system block, rules has the rules
    systems = {arm: {s for (s, _) in reader.calls[i::3]} for i, arm in enumerate(("control", "rules", "placebo"))}
    # same prefix (format + tool list) in every arm; control carries no guidance block at all
    assert all(s.startswith(rg._SYSTEM_PREFIX) for arm in systems for s in systems[arm])
    assert systems["control"] == {rg._SYSTEM_PREFIX} and all("confirm exact paths" in s for s in systems["rules"])
    assert all("Standing guidance" not in s for s in systems["control"])
    v = rg.verdict(rows, cfg)
    assert v["arms"]["control"]["solve_rate"] == 1.0 and v["arms"]["placebo"]["solve_rate"] == 1.0
    assert v["arms"]["rules"]["solve_rate"] < 0.95
    assert v["b_r3"]["status"] == "fail" and v["b_r3"]["delta_pp"] < -5.0 and v["b_r3"]["paired"]["rules_worse"] > 0
    assert v["b_r4"]["status"] == "harm"
    assert v["prereg_digest"] == rg.prereg_digest(cfg)
    # a harmless rule block passes the gate and reads null against placebo
    reader2 = _Reader({})
    rows2 = rg.run_arms(cfg, reader2, tmp_path / "prereg.json")
    v2 = rg.verdict(rows2, cfg)
    assert v2["b_r3"]["status"] == "pass" and v2["b_r4"]["status"] == "null"


def test_cli_preregisters_then_runs_offline_fake(tmp_path, monkeypatch):
    from telarch_evals import cli
    inv = tmp_path / "inv.json"
    inv.write_text(json.dumps(INVARIANTS))
    monkeypatch.setattr(cli, "ChatClient", lambda *a, **k: _Reader({}))
    monkeypatch.setenv("TELARCH_EVALS_HOME", str(tmp_path / "home"))
    rc = cli.main(["rule-gate", "--invariants", str(inv), "--n-tasks", "12", "--seed", "2", "--reader-model", "fake/reader",
                   "--prereg", str(tmp_path / "pre.json"), "--preregister-only"])
    assert rc == 0 and (tmp_path / "pre.json").exists()
    rc = cli.main(["rule-gate", "--invariants", str(inv), "--n-tasks", "12", "--seed", "2", "--reader-model", "fake/reader",
                   "--prereg", str(tmp_path / "pre.json"), "--no-cache"])
    assert rc == 0
    runs = list((tmp_path / "home" / "runs" / "rule_gate").iterdir())
    assert len(runs) == 1
    summary = json.loads((runs[0] / "summary.json").read_text())
    assert summary["b_r3"]["status"] == "pass" and summary["prereg_digest"]
    # a changed seed against the same prereg refuses to launch
    rc = cli.main(["rule-gate", "--invariants", str(inv), "--n-tasks", "12", "--seed", "3", "--reader-model", "fake/reader",
                   "--prereg", str(tmp_path / "pre.json"), "--no-cache"])
    assert rc == 2
