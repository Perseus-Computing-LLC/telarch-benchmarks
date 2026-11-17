"""The sealed holdout: a fixed partition of each benchmark and a ledger receipt for every holdout read.

Before this change the harness had no dev/holdout split: every screen and every paid run read the whole question
set, so nothing was left to confirm a tuned setting on, and the ledger did not say how often a set had been read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from telarch_evals import bench_beam, bench_locomo, bench_longmemeval, holdout
from telarch_evals.cli import main
from telarch_evals.memory import TelarchMemory
from telarch_evals.runlog import verify
from test_offline import FakeBeam, FakeClient, FakeJudgePrompt, FakeLongMemEval, _ctx


def test_partition_is_a_fixed_stratified_share():
    groups = {f"q{i}": ("a" if i < 50 else "b") for i in range(70)}       # strata of 50 and 20
    part = holdout.partition("bench", groups)
    assert len(part.holdout) == 15 + 6 and len(part.groups) == 70
    assert sum(g in part.holdout for g in groups if groups[g] == "a") == 15
    # Order of the input does not matter, the seed and the benchmark name do.
    assert holdout.partition("bench", dict(reversed(list(groups.items())))).holdout == part.holdout
    assert holdout.partition("bench", groups, seed=1).holdout != part.holdout
    assert holdout.partition("other", groups).holdout != part.holdout
    assert part.select("dev") == part.groups - part.holdout and part.select(None) == part.groups
    assert part.digest == holdout.partition("bench", groups).digest and len(part.digest) == 64
    # Rounded half up per stratum: 10 groups give 3, 5 give 2 (1.5), 1 gives 0.
    assert [len(holdout.partition("b", [str(i) for i in range(n)]).holdout) for n in (10, 5, 1)] == [3, 2, 0]
    with pytest.raises(ValueError):
        holdout.partition("b", ["x", "x"])


def _lme(tmp_path: Path) -> Path:
    session = [{"role": "user", "content": "I adopted a beagle named Biscuit.", "has_answer": True},
               {"role": "assistant", "content": "Congratulations!"}]
    base = {"question_date": "2023/06/01 (Thu) 10:00", "haystack_session_ids": ["s1"],
            "haystack_dates": ["2023/05/01 (Mon) 09:00"], "haystack_sessions": [session],
            "question": "What breed is Biscuit?", "answer": "beagle", "answer_session_ids": ["s1"]}
    data = [{**base, "question_id": f"q{i}", "question_type": "type-a" if i < 5 else "type-b"} for i in range(10)]
    path = tmp_path / "lme.json"
    path.write_text(json.dumps(data))
    return path


def _prereg(tmp_path: Path, digest: str, name: str = "prereg.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"prereg": {"primary_metric": "overall_accuracy", "threshold": "interval excludes 0"},
                                "holdout_digest": digest}))
    return path


def _lme_run(tmp_path: Path, **kw):
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), FakeClient("reader", "beagle"),
               FakeClient("gpt-4o-2024-08-06", "yes"))
    for key, value in kw.items():
        setattr(ctx, key, value)
    return ctx, bench_longmemeval.run(ctx, data_path=_lme(tmp_path), official=FakeLongMemEval())


def test_longmemeval_dev_run_reads_no_holdout_question(tmp_path: Path):
    _, out = _lme_run(tmp_path, partition="dev")
    rows = [json.loads(line) for line in (out / "judgments.jsonl").read_text().splitlines()]
    manifest = json.loads((out / "manifest.json").read_text())
    data = json.loads((tmp_path / "lme.json").read_text())
    part = holdout.partition("longmemeval/lme.json", {e["question_id"]: e["question_type"] for e in data})
    assert len(part.holdout) == 4                                    # 2 of each type of 5
    assert len(rows) == 6 and not {r["id"] for r in rows}.intersection(part.holdout)
    assert manifest["config"]["partition"]["which"] == "dev"
    assert manifest["config"]["partition"]["holdout_digest"] == part.digest
    assert holdout.reads(tmp_path / "runs") == []                    # a dev run leaves no receipt


def test_an_unpartitioned_run_is_recorded_as_a_holdout_read(tmp_path: Path):
    ctx, out = _lme_run(tmp_path)
    assert "partition" not in json.loads((out / "manifest.json").read_text())["config"]  # earlier runs still resume
    (receipt,) = holdout.reads(tmp_path / "runs")
    assert receipt["run_id"] == out.name and receipt["partition"] == "unpartitioned"
    assert receipt["holdout_groups_read"] == 4 == receipt["holdout_groups_total"]
    assert receipt["questions_selected"] == 10 and receipt["prereg_sha256"] == "none"
    # A resume does not count as a second read, and the run ledger still verifies.
    ctx.resume = out
    bench_longmemeval.run(ctx, data_path=_lme(tmp_path), official=FakeLongMemEval())
    assert len(holdout.reads(tmp_path / "runs")) == 1
    assert verify(tmp_path / "runs")["valid"]


def test_a_holdout_run_needs_a_pre_registration_for_this_partition(tmp_path: Path):
    data = json.loads(_lme(tmp_path).read_text())
    part = holdout.partition("longmemeval/lme.json", {e["question_id"]: e["question_type"] for e in data})
    with pytest.raises(SystemExit, match="needs --holdout-prereg"):
        _lme_run(tmp_path, partition="holdout")
    with pytest.raises(SystemExit, match="another holdout"):
        _lme_run(tmp_path, partition="holdout", prereg=_prereg(tmp_path, "0" * 64))
    no_threshold = tmp_path / "bad.json"
    no_threshold.write_text(json.dumps({"prereg": {"primary_metric": "x"}, "holdout_digest": part.digest}))
    with pytest.raises(SystemExit, match="lacks threshold"):
        _lme_run(tmp_path, partition="holdout", prereg=no_threshold)
    with pytest.raises(SystemExit, match="goes with --partition holdout"):
        _lme_run(tmp_path, partition="dev", prereg=_prereg(tmp_path, part.digest))
    assert not (tmp_path / "runs" / "longmemeval").exists()          # a refused run creates nothing

    prereg = _prereg(tmp_path, part.digest)
    _, out = _lme_run(tmp_path, partition="holdout", prereg=prereg)
    rows = [json.loads(line) for line in (out / "judgments.jsonl").read_text().splitlines()]
    assert {r["id"] for r in rows} == set(part.holdout)
    manifest = json.loads((out / "manifest.json").read_text())
    (receipt,) = holdout.reads(tmp_path / "runs")
    assert receipt["partition"] == "holdout" and receipt["prereg_file"] == "prereg.json"
    assert receipt["prereg_sha256"] == manifest["config"]["partition"]["prereg_sha256"] == holdout.check_prereg(prereg, part)
    assert {"name": "prereg", "file": "prereg.json", "sha256": receipt["prereg_sha256"]} in manifest["sources"]


def test_locomo_and_beam_hold_out_whole_conversations(tmp_path: Path):
    def sample(i: int) -> dict:
        return {"sample_id": f"conv-{i}", "conversation": {
            "speaker_a": "Ann", "speaker_b": "Bo", "session_1_date_time": "1:00 pm on 8 May, 2023",
            "session_1": [{"speaker": "Ann", "dia_id": "D1:1", "text": "I ran a marathon in Boston last week."}]},
            "qa": [{"question": "Which city did Ann run a marathon in?", "answer": "Boston", "evidence": ["D1:1"],
                    "category": 4}]}

    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample(i) for i in range(10)]))
    part = holdout.partition("locomo/locomo.json", [f"conv-{i}" for i in range(10)])
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), FakeClient("reader", "Boston"),
               FakeClient("gpt-4o-mini", '{"label": "CORRECT"}'))
    ctx.partition = "dev"
    out = bench_locomo.run(ctx, data_path=path, judge_prompt=FakeJudgePrompt())
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(part.holdout) == 3 and set(manifest["config"]["conversations"]) == set(part.groups - part.holdout)
    assert holdout.reads(tmp_path / "runs") == []
    ctx.partition = None
    bench_locomo.run(ctx, data_path=path, judge_prompt=FakeJudgePrompt())
    assert [r["holdout_groups_read"] for r in holdout.reads(tmp_path / "runs")] == [3]

    turns = [[{"role": "user", "content": "I am moving to Lisbon in July.", "time_anchor": "March-15-2024"},
              {"role": "assistant", "content": "Lisbon is lovely in summer."}]]
    for i in range(1, 5):
        conv = tmp_path / "beam" / str(i)
        (conv / "probing_questions").mkdir(parents=True)
        (conv / "chat.json").write_text(json.dumps([{"batch_number": 1, "turns": turns}]))
        (conv / "probing_questions" / "probing_questions.json").write_text(json.dumps({
            "information_extraction": [{"question": "Where am I moving?", "rubric": ["Lisbon"]}]}))
    beam_part = holdout.partition("beam/100K/beam", ["1", "2", "3", "4"])
    ctx = _ctx(tmp_path / "b", TelarchMemory(tmp_path / "b" / "work"), FakeClient("reader", "Lisbon"),
               FakeClient("gpt-4.1-mini", "{}"))
    ctx.partition = "holdout"
    ctx.prereg = _prereg(tmp_path, beam_part.digest, "beam-prereg.json")
    out = bench_beam.run(ctx, data_dir=tmp_path / "beam", official=FakeBeam(), abilities=["information_extraction"])
    rows = [json.loads(line) for line in (out / "judgments.jsonl").read_text().splitlines()]
    assert len(beam_part.holdout) == 1 and {r["conversation"] for r in rows} == set(beam_part.holdout)
    (receipt,) = holdout.reads(tmp_path / "b" / "runs")
    assert receipt["partition_benchmark"] == "beam/100K/beam" and receipt["questions_selected"] == 1


def test_holdout_reads_command_counts_receipts(tmp_path: Path, capsys):
    _lme_run(tmp_path)
    assert main(["holdout", "reads", "--runs-dir", str(tmp_path / "runs")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reads"] == 1 and report["prereg_reads"] == 0
    assert main(["holdout", "reads", "--runs-dir", str(tmp_path / "nothing-here")]) == 0
    assert json.loads(capsys.readouterr().out)["reads"] == 0
