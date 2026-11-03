"""How the reader sees the telarch system's packed rounds (telarch phase-P14, item P14.4).

--pack flat, the default, renders each packed round in full, as every run before the knob did. --pack two-pass
renders each one as an index row: its title, the key Kibisis stores it under (the round's uid), and its description,
kibisis.okf.export.first_sentence of its stored text (P13.1's description), as "- [uid] description" (the title alone
when there is no description). It then expands to the whole round each row the question names, in the order the
question names them, while it fits the same budget: kibisis.okf.export.named_rows over the question with its turn
scaffolding removed, and harpe.two_pass.TwoPassBuild (P14.3's rule, as the kibisis-vault plugin applies it). The
selection is the flat selection; only the rendering differs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from kibisis.okf.export import first_sentence

from telarch_evals import bench_longmemeval
from telarch_evals.memory import TelarchMemory
from test_max_items import CONFIG_KEYS_325C393, QUESTION, _entry, _recall, _run

# The fixture's prompt (_entry("q"), the default telarch system, retrieval only) as telarch-benchmarks 7693e99 renders
# it with kibisis 0.1.0a14 and harpe 0.1.0a12, the versions pyproject pins: flat reproduces it byte for byte.
FLAT_PROMPT_SHA256_7693E99 = "305575564dbdaa6ad4aae1f743f6fc57fd401c31ed24bc25fd43b2f468b86f43"
FLAT_PROMPT_TOKENS_7693E99 = 6887


def _row(tmp_path: Path, system: TelarchMemory, entry: dict) -> tuple[dict, dict]:
    """The one judgments row of a retrieval-only run over `entry`, and the run's manifest."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out, _ = _run(tmp_path, system, entry)
    rows = [json.loads(x) for x in (out / "judgments.jsonl").read_text().splitlines() if x]
    assert len(rows) == 1 and rows[0]["status"] == "ok"
    return rows[0], json.loads((out / "manifest.json").read_text())


def _asking(question: str) -> dict:
    entry = _entry("q")
    entry["question"] = question
    return entry


def _full(recall) -> list[str]:
    """The uids of the rounds a recall hands the reader whole."""
    return [u.uid for u in recall.units if u.index_row is None]


def test_the_default_reproduces_the_prompt_before_the_knob(tmp_path):
    row, _ = _row(tmp_path / "run", TelarchMemory(tmp_path / "run" / "w"), _entry("q"))
    assert (row["prompt_sha256"], row["prompt_tokens_o200k"], row["memory"]["selected"]) \
        == (FLAT_PROMPT_SHA256_7693E99, FLAT_PROMPT_TOKENS_7693E99, 12)
    assert list(TelarchMemory(tmp_path / "c").config()) == CONFIG_KEYS_325C393


def test_flat_is_the_default(tmp_path):
    row, manifest = _row(tmp_path / "run", TelarchMemory(tmp_path / "run" / "w", pack="flat"), _entry("q"))
    assert row["prompt_sha256"] == FLAT_PROMPT_SHA256_7693E99
    assert "two_pass" not in row["memory"] and "pack" not in manifest["config"]["memory"]
    assert list(TelarchMemory(tmp_path / "c", pack="flat").config()) == CONFIG_KEYS_325C393
    assert list(TelarchMemory(tmp_path / "d", pack="two-pass").config()) == CONFIG_KEYS_325C393 + ["pack"]


def test_two_pass_renders_each_packed_round_as_its_title_and_description(tmp_path):
    flat = _recall(TelarchMemory(tmp_path / "flat"), _entry("q"))
    two = _recall(TelarchMemory(tmp_path / "two", pack="two-pass"), _entry("q"))
    assert [u.uid for u in two.units] == [u.uid for u in flat.units] and len(two.units) == 12
    assert all(u.index_row is None for u in flat.units)
    for unit in two.units:
        assert unit.index_row == f"- [{unit.uid}] {first_sentence(unit.text)}"
    history = bench_longmemeval.render_history(two.units)
    assert all(f"\n\n{u.index_row}" in history for u in two.units)
    assert "Note 2-0-0" not in history and "assistant:" not in history
    assert history.count("### Session") == 6
    assert two.stats["joined_tokens_o200k"] * 4 < flat.stats["joined_tokens_o200k"]
    assert (two.stats["two_pass"]["rows_pass1"], two.stats["two_pass"]["rows_expanded"]) == (12, 0)


def test_two_pass_expands_the_rounds_the_question_names_in_the_order_it_names_them(tmp_path):
    two = _recall(TelarchMemory(tmp_path / "w", pack="two-pass"), _asking(f"{QUESTION} What did I say in 4:1 and 2:0?"))
    assert sorted(_full(two)) == ["2:0", "4:1"]
    stats = two.stats["two_pass"]
    assert stats["expanded"] == ["4:1", "2:0"]
    assert (stats["named"], stats["rows_expanded"], stats["expansions_refused"]) == (2, 2, 0)
    assert stats["budget_tokens"] == 8000 and 0 < stats["used_tokens"] <= 8000
    history = bench_longmemeval.render_history(two.units)
    assert "Note 4-1-24: the Pine Hollow path passes stone marker 24 and a bench." in history
    assert "Note 3-1-24" not in history


def test_a_named_round_outside_the_selection_stays_out(tmp_path):
    names = " ".join(f"{s}:{r}" for s in range(6) for r in range(2))
    entry = _asking(f"{QUESTION} What did I say in {names}?")
    flat = _recall(TelarchMemory(tmp_path / "flat", budget=1500), entry)
    two = _recall(TelarchMemory(tmp_path / "two", budget=1500, pack="two-pass"), entry)
    assert [u.uid for u in two.units] == [u.uid for u in flat.units] and 0 < len(two.units) < 12
    assert _full(two) == [u.uid for u in two.units]
    assert two.stats["two_pass"]["named"] == two.stats["two_pass"]["rows_expanded"] == len(two.units)
    assert bench_longmemeval.render_history(two.units) == bench_longmemeval.render_history(flat.units)


def test_names_inside_turn_scaffolding_do_not_expand(tmp_path):
    question = f"{QUESTION} What did I say in 3:0?\n```\nsee 1:1\n```\n> and 5:0\n\U0001F4C4 0:1 notes.txt"
    two = _recall(TelarchMemory(tmp_path / "w", pack="two-pass"), _asking(question))
    assert _full(two) == ["3:0"] and two.stats["two_pass"]["named"] == 1


def test_two_pass_prompts_repeat_byte_for_byte(tmp_path):
    a, manifest = _row(tmp_path / "a", TelarchMemory(tmp_path / "a" / "w", pack="two-pass"), _entry("q"))
    b, _ = _row(tmp_path / "b", TelarchMemory(tmp_path / "b" / "w", pack="two-pass"), _entry("q"))
    assert a["prompt_sha256"] == b["prompt_sha256"] != FLAT_PROMPT_SHA256_7693E99
    assert a["prompt_tokens_o200k"] * 4 < FLAT_PROMPT_TOKENS_7693E99
    assert a["memory"]["two_pass"]["rows_pass1"] == 12
    assert a["evidence"]["session_recall"] == a["evidence"]["round_recall"] == 1.0
    assert manifest["config"]["memory"]["pack"].startswith("two-pass")


def test_the_render_puts_an_index_row_where_its_rounds_turns_were():
    units = bench_longmemeval.units_for(_entry("q"))[:3]  # session 0 rounds 0 and 1, session 1 round 0
    flat, _ = bench_longmemeval.render_history_spans(units)
    turns = "".join("\n\n{}: {}".format(t["role"], t["content"].strip()) for t in units[1].turns)
    two, spans = bench_longmemeval.render_history_spans([units[0], replace(units[1], index_row="- [0:1] row"),
                                                          units[2]])
    assert turns in flat and two == flat.replace(turns, "\n\n- [0:1] row")
    assert sorted(spans.values()) == [(0, spans[0][1]), (spans[0][1], len(two))]


def test_the_cli_hands_the_pack_to_the_telarch_system_on_longmemeval(tmp_path, monkeypatch):
    from telarch_evals import cli

    seen = []

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
    assert cli.main(args + ["--pack", "two-pass"]) == 0
    assert cli.main(args + ["--pack", "flat"]) == 0
    assert cli.main(args) == 0
    assert seen[0].system.config()["pack"].startswith("two-pass")
    assert "pack" not in seen[1].system.config() and "pack" not in seen[2].system.config()
    for other in (["--system", "full-context"], ["--pair-window", "192"], ["--window", "128"],
                  ["--passthrough-under", "4000"]):
        with pytest.raises(SystemExit, match="--pack two-pass"):
            cli.main(args + ["--pack", "two-pass"] + other)
    with pytest.raises(SystemExit, match="--pack two-pass"):
        cli.main(["locomo"] + args[1:] + ["--pack", "two-pass"])
    assert len(seen) == 3


def test_the_system_refuses_an_unknown_pack_and_two_pass_over_windows(tmp_path):
    refused: list[dict[str, Any]] = [{"pack": "three-pass"}, {"pack": "two-pass", "pair_window": 192},
                                     {"pack": "two-pass", "window": 128}, {"pack": "two-pass", "passthrough_under": 4000}]
    for i, kwargs in enumerate(refused):
        with pytest.raises(ValueError):
            TelarchMemory(tmp_path / f"w{i}", **kwargs)
