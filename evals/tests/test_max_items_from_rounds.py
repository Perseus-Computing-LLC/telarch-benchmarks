"""A run-time item count (telarch phase-P12, item P12.1).

--max-items-from-rounds caps a windowed packer (--pair-window or --window) at N_q, computed in the run for each
question: the number of whole rounds the round packer (the control's packing: no --pair-window, no --window)
selects for the same search and ranking at the run's budget, after its own render fit, so N_q is what the control
arm's recall reports as `selected`. The cap then works as --max-items does (P11.1): after packing and the render
fit, keep the N_q packed items of highest packing relevance, in packed order. On any history the arm therefore
equals --max-items-file holding the control's selected counts, without the file; the run config records the mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.memory import TelarchMemory

from test_max_items import CONFIG_KEYS_325C393, DATE, QUESTION, TRAILS, _entry, _recall, _run, _write

PAIRS = {"pair_window": 192, "scoring": "mix"}
WINDOWS = {"window": 128}


def _ramp(qid: str = "ramp", n: int = 24) -> dict:
    """24 one-round sessions whose replies run from 1 to 25 notes: rounds of very different sizes and a session
    header per round, so at budgets 3000 and 2000 the round packer's render fit packs again (reserved > 0) and the
    pair packer still selects more items than the round packer."""
    sessions = []
    for s in range(n):
        trail = TRAILS[s % len(TRAILS)]
        mentions = " ".join(["hiking trail by the lake"] * (1 + s % 3))
        reply = " ".join(f"Note {s}-{k}: the {trail} path passes stone marker {k} and a bench."
                         for k in range(1 + (s * 7) % 25))
        sessions.append([{"role": "user", "content": f"Session {s}: I walked the {trail} {mentions} today."},
                         {"role": "assistant", "content": reply}])
    sessions[2][0]["has_answer"] = True
    ids = [f"{qid}_s{i}" for i in range(n)]
    return {"question_id": qid, "question_type": "single-session-user", "question": QUESTION, "answer": "Mirror Lake",
            "question_date": DATE, "haystack_session_ids": ids,
            "haystack_dates": [f"2023/05/{1 + i % 28:02d} (Mon) 09:00" for i in range(n)],
            "haystack_sessions": sessions, "answer_session_ids": [ids[2]]}


class FirstPack(TelarchMemory):
    """The control, remembering how many rounds its first pack held (before the render fit packs again)."""

    first = None

    def _pack(self, history_id, ranked, by_uid, question, budget=None):
        report, n = super()._pack(history_id, ranked, by_uid, question, budget)
        if type(self).first is None:
            type(self).first = len(report.included_items) if report else 0
        return report, n


def _timeless(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if not k.endswith("_ms")}


# (fixture, budget, packer, the cap bites, the control's render fit packs again, and changes the control's count)
CASES = [
    ("trails", 3000, PAIRS, True, False, False),
    ("trails", 1500, PAIRS, True, False, False),
    ("trails", 900, PAIRS, True, False, False),
    ("trails", 8000, PAIRS, False, False, False),
    ("ramp", 3000, PAIRS, True, True, True),
    ("ramp", 2000, PAIRS, True, True, False),
    ("trails", 3000, WINDOWS, True, False, False),
]


@pytest.mark.parametrize("fixture, budget, packer, bites, refit, recount", CASES)
def test_n_q_is_the_controls_selected_count_and_the_arm_equals_the_file_capped_arm(tmp_path, fixture, budget, packer,
                                                                                  bites, refit, recount):
    entry = _entry(fixture) if fixture == "trails" else _ramp(fixture)
    qid = entry["question_id"]
    FirstPack.first = None
    control = _recall(FirstPack(tmp_path / "control", budget=budget), entry)
    n_q = control.stats["selected"]
    assert n_q >= 1
    assert (control.stats["fit_reserved"] > 0) == refit, "the fixture no longer exercises the control's render fit"
    assert (FirstPack.first != n_q) == recount, "the control's first pack no longer differs from N_q (or now does)"
    filed = _recall(TelarchMemory(tmp_path / "filed", budget=budget, **packer,
                                  max_items_file=_write(tmp_path / "caps.json", {qid: n_q})), entry)
    run_time = _recall(TelarchMemory(tmp_path / "run-time", budget=budget, **packer, max_items_from_rounds=True), entry)
    assert run_time.stats["max_items"] == n_q
    assert (run_time.stats["capped"] > 0) == bites, "the fixture no longer makes the cap bite (or no longer not)"
    assert [u.uid for u in run_time.units] == [u.uid for u in filed.units]
    assert bench_longmemeval.render_history(run_time.units) == bench_longmemeval.render_history(filed.units)
    assert _timeless(run_time.stats) == _timeless(filed.stats)


def test_a_control_that_selects_no_round_caps_the_arm_at_none(tmp_path):
    entry = _entry("none")
    control = _recall(TelarchMemory(tmp_path / "control", budget=400), entry)
    uncapped = _recall(TelarchMemory(tmp_path / "uncapped", budget=400, **PAIRS), entry)
    assert control.stats["selected"] == 0 and uncapped.stats["selected"] >= 1, "the fixture changed"
    run_time = _recall(TelarchMemory(tmp_path / "run-time", budget=400, **PAIRS, max_items_from_rounds=True), entry)
    assert run_time.units == []
    assert (run_time.stats["selected"], run_time.stats["max_items"]) == (0, 0)
    assert run_time.stats["capped"] == uncapped.stats["selected"]
    assert run_time.stats["context_tokens"] == 0


def test_the_config_records_the_mode_and_no_file(tmp_path):
    cfg = TelarchMemory(tmp_path / "p", **PAIRS, max_items_from_rounds=True).config()
    assert cfg["max_items_from"] == "rounds"
    assert "max_items" not in cfg and "max_items_file" not in cfg
    assert list(cfg)[:-1] == CONFIG_KEYS_325C393 + ["pair_window", "scoring"]
    assert "max_items_from" not in TelarchMemory(tmp_path / "off", **PAIRS).config()


def test_a_retrieval_only_run_equals_the_file_capped_run_prompt_for_prompt(tmp_path):
    entries = (_entry("qa"), _ramp("qb"))

    def rows(out: Path) -> dict:
        return {r["id"]: r for r in map(json.loads, (out / "judgments.jsonl").read_text().splitlines()) if r}

    control, _ = _run(tmp_path, TelarchMemory(tmp_path / "wc", budget=3000), *entries, root="control")
    caps = {q: r["memory"]["selected"] for q, r in rows(control).items()}
    filed, _ = _run(tmp_path, TelarchMemory(tmp_path / "wf", budget=3000, **PAIRS,
                                             max_items_file=_write(tmp_path / "caps.json", caps)), *entries, root="filed")
    run_time, _ = _run(tmp_path, TelarchMemory(tmp_path / "wr", budget=3000, **PAIRS, max_items_from_rounds=True),
                       *entries, root="run-time")
    f, r = rows(filed), rows(run_time)
    assert sorted(r) == sorted(caps) == ["qa", "qb"]
    for q in caps:
        assert r[q]["status"] == "ok"
        assert r[q]["prompt_sha256"] == f[q]["prompt_sha256"]
        assert r[q]["memory"]["selected"] == f[q]["memory"]["selected"]
        assert r[q]["memory"]["max_items"] == caps[q]
    assert any(r[q]["memory"]["capped"] for q in caps), "no question was capped"
    manifest = json.loads((run_time / "manifest.json").read_text())
    assert manifest["config"]["memory"]["max_items_from"] == "rounds"
    assert "max_items_file" not in manifest["config"]["memory"]


@pytest.mark.parametrize("knob, match", [
    ({"max_items": 3}, "one cap at a time"),
    ({"surface_conflicts": True}, "packed items"),
    ({"passthrough_under": 4000}, "packed items"),
    ({"fill_floor": 0.5}, "fill-floor"),
])
def test_the_run_time_cap_is_rejected_where_it_is_not_defined(tmp_path, knob, match):
    with pytest.raises(ValueError, match=match):
        TelarchMemory(tmp_path / "x", **PAIRS, max_items_from_rounds=True, **knob)


def test_the_run_time_cap_takes_no_file_and_needs_a_windowed_packer(tmp_path):
    caps = _write(tmp_path / "caps.json", {"q": 3})
    with pytest.raises(ValueError, match="one cap at a time"):
        TelarchMemory(tmp_path / "f", **PAIRS, max_items_from_rounds=True, max_items_file=caps)
    with pytest.raises(ValueError, match="windowed packer"):
        TelarchMemory(tmp_path / "rounds", max_items_from_rounds=True)


def test_the_cli_hands_the_run_time_cap_to_the_telarch_system(tmp_path, monkeypatch):
    from telarch_evals import cli

    seen: list[Context] = []

    def fake_run(ctx, **kw):
        seen.append(ctx)
        out = tmp_path / f"run{len(seen)}"
        out.mkdir()
        (out / "summary.json").write_text(json.dumps({"benchmark": "longmemeval", "rows": 0, "errors": 0,
                                                      "complete": True}))
        return out

    monkeypatch.setattr(bench_longmemeval, "run", fake_run)
    monkeypatch.setenv("TELARCH_EVALS_HOME", str(tmp_path / "home"))
    args = ["longmemeval", "--api-key-env", "NONE", "--retrieval-only", "--runs-dir", str(tmp_path / "runs")]
    assert cli.main(args + ["--pair-window", "192", "--scoring", "mix", "--max-items-from-rounds"]) == 0
    assert cli.main(args + ["--pair-window", "192", "--scoring", "mix"]) == 0
    assert seen[0].system.config()["max_items_from"] == "rounds"
    assert "max_items_from" not in seen[1].system.config()
    with pytest.raises(SystemExit):
        cli.main(args + ["--max-items-from-rounds", "--system", "full-context"])
    with pytest.raises(ValueError, match="one cap at a time"):
        cli.main(args + ["--pair-window", "192", "--max-items-from-rounds", "--max-items", "4"])
