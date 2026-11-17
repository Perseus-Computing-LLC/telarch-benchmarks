"""Noise floor, speaker share and the abstention audit.

Noise floor: before this change a candidate's gain over its control was read against a bootstrap interval alone.
A replay of the control's own contexts (same prompts, fresh reader and judge draws) shows how many questions change
with no change to memory at all; LoCoMo and BEAM rows carried no prompt digest, so a replay could not be shown to
have had the control's contexts.

Speaker share: nothing recorded whose words fill the packed context.

Abstention audit: nothing compared a judge's credit on an abstention question with whether the answer declined.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from telarch_evals import abstention_audit, bench_beam, bench_locomo, bench_longmemeval, noise_floor
from telarch_evals.common import speaker_share, speaker_summary
from telarch_evals.memory import TelarchMemory, Unit
from test_offline import FakeBeam, FakeClient, FakeJudgePrompt, FakeLongMemEval, _ctx, _lme_data, _text


def _run(tmp_path: Path, name: str, benchmark: str, rows: list[dict], **config) -> Path:
    run = tmp_path / name
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"benchmark": benchmark, "run_id": name, "repo_commit": "abc",
                                                   "config": {"system": "telarch", "memory": {"budget": 8000},
                                                              **config}}))
    (run / "judgments.jsonl").write_text("".join(json.dumps({"status": "ok", **r}) + "\n" for r in rows))
    return run


def _locomo_rows(js: list[int], prompt: str = "p", answers: str = "a") -> list[dict]:
    return [{"id": f"c/{i}", "j": j, "category_name": "single-hop" if i % 2 else "temporal", "answer": f"{answers}{i}",
             "prompt_sha256": f"{prompt}{i}"} for i, j in enumerate(js)]


def test_a_replay_of_identical_contexts_sets_the_floor(tmp_path: Path):
    control = _run(tmp_path, "control", "locomo", _locomo_rows([1, 1, 1, 0, 0, 0, 1, 0, 1, 0]))
    #                                                     flips: q0 down, q3 up, q4 up
    replay = _run(tmp_path, "replay", "locomo", _locomo_rows([0, 1, 1, 1, 1, 0, 1, 0, 1, 0], answers="b"),
                  no_cache=True)
    # The candidate changed four prompts; q9 went up on a changed prompt, q1 went down on an unchanged one.
    cand_rows = _locomo_rows([1, 0, 1, 0, 0, 0, 1, 0, 1, 1])
    for row in cand_rows[6:]:
        row["prompt_sha256"] += "-new"
    candidate = _run(tmp_path, "cand", "locomo", cand_rows, memory={"budget": 4000})
    out = noise_floor.record(control, [replay], [candidate])
    assert out["valid"] and out["problems"] == []
    (r,) = out["replays"]
    assert (r["n"], r["up"], r["down"], r["changed"], r["net"]) == (10, 2, 1, 3, 1)
    assert r["prompts"] == {"same": 10, "changed": 0, "no_digest": 0} and r["answers_changed"] == 10
    assert r["mean_base_bp"] == 5000 and r["delta_bp"] == 1000 and r["resampled"] == "reader and judge"
    assert r["by_group"]["temporal"] == {"n": 5, "up": 1, "down": 1}
    assert out["floor"] == {"changed": 3, "abs_net": 1, "abs_delta_bp": 1000, "replays": 1}
    (c,) = out["candidates"]
    assert (c["up"], c["down"], c["delta_bp"]) == (1, 1, 0) and c["same_question_set"]
    assert c["on_same_prompt"] == {"up": 0, "down": 1} and c["on_changed_prompt"] == {"up": 1, "down": 0}
    assert c["within_floor"] and "inside the noise floor" in c["reading"]


def test_a_run_that_is_not_a_replay_is_refused(tmp_path: Path):
    control = _run(tmp_path, "control", "locomo", _locomo_rows([1, 0, 1]))
    cached = _run(tmp_path, "cached", "locomo", _locomo_rows([1, 0, 1]))
    other_prompts = _run(tmp_path, "other", "locomo", _locomo_rows([1, 0, 1], prompt="x"), no_cache=True)
    other_settings = _run(tmp_path, "settings", "locomo", _locomo_rows([1, 0, 1]), no_cache=True, memory={"budget": 1})
    no_digest = _run(tmp_path, "nodigest", "locomo",
                     [{k: v for k, v in r.items() if k != "prompt_sha256"} for r in _locomo_rows([1, 0, 1])],
                     no_cache=True)
    for run, text in ((cached, "re-sampled nothing"), (other_prompts, "3 reader prompts differ"),
                      (other_settings, "settings differ"), (no_digest, "no prompt_sha256")):
        out = noise_floor.record(control, [run], [cached])
        assert not out["valid"] and any(text in p for p in out["problems"]), (run.name, out["problems"])
        assert "within_floor" not in out["candidates"][0]          # no floor, no reading


def test_scores_and_sign_test():
    assert noise_floor.score("longmemeval", {"autoeval_label": {"label": True}}) == 1.0
    assert noise_floor.score("beam", {"score": None}) == 0.0 and noise_floor.score("beam", {"score": 0.5}) == 0.5
    assert noise_floor.sign_test_p(0, 0) == 1.0 and noise_floor.sign_test_p(5, 0) == 0.0625
    assert noise_floor.sign_test_p(3, 3) == 1.0 and round(noise_floor.sign_test_p(9, 1), 6) == 0.021484
    with pytest.raises(SystemExit):
        noise_floor.score("memoryagentbench", {})


def _locomo_data(tmp_path: Path) -> Path:
    sample = {"sample_id": "conv-1", "conversation": {
        "speaker_a": "Ann", "speaker_b": "Bo", "session_1_date_time": "1:00 pm on 8 May, 2023",
        "session_1": [{"speaker": "Ann", "dia_id": "D1:1", "text": "I ran a marathon in Boston last week."},
                      {"speaker": "Bo", "dia_id": "D1:2", "text": "Amazing, a marathon in Boston!"}]},
        "qa": [{"question": "Which city did Ann run a marathon in?", "answer": "Boston", "evidence": ["D1:1"],
                "category": 4}]}
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]))
    return path


def test_a_no_cache_rerun_is_a_valid_replay_and_rows_carry_speaker_share(tmp_path: Path):
    judge = FakeClient("gpt-4o-mini", lambda m: '{"label": "CORRECT"}' if "Boston|Boston" in _text(m)
                       else '{"label": "WRONG"}')
    control_ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), FakeClient("reader", "Boston"), judge)
    control = bench_locomo.run(control_ctx, data_path=_locomo_data(tmp_path), judge_prompt=FakeJudgePrompt())
    replay_ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work2"), FakeClient("reader", "Paris"), judge)
    replay_ctx.no_cache = True
    replay = bench_locomo.run(replay_ctx, data_path=_locomo_data(tmp_path), judge_prompt=FakeJudgePrompt())
    out = noise_floor.record(control, [replay])
    assert out["valid"], out["problems"]
    assert out["replays"][0]["down"] == 1 and out["replays"][0]["prompts"]["same"] == 1

    row = json.loads((control / "judgments.jsonl").read_text().splitlines()[0])
    assert len(row["prompt_sha256"]) == 64
    assert row["memory"]["speaker_chars"] == {"Ann": 37, "Bo": 30}       # both turns were packed
    assert row["memory"]["speaker_share_bp"] == {"Ann": 5522, "Bo": 4478}
    assert row["memory"]["history_speaker_share_bp"] == {"Ann": 5522, "Bo": 4478}
    summary = json.loads((control / "summary.json").read_text())
    assert summary["speaker_share"]["pooled_share_bp"] == {"Ann": 5522, "Bo": 4478}
    assert summary["speaker_share"]["questions"] == 1


def test_speaker_share_counts_roles_and_index_rows():
    def unit(uid, turns, index_row=None):
        return Unit(uid=uid, session="s", order=(0, 0), date="", text="", rendered="", turns=turns, index_row=index_row)

    history = [unit("a", [{"role": "user", "content": "x" * 10}, {"role": "assistant", "content": "y" * 90}]),
               unit("b", [{"role": "user", "content": "x" * 100}]),
               unit("c", [{"role": "assistant", "content": "y" * 800}])]
    got = speaker_share(history[:2], history)
    assert got["speaker_chars"] == {"assistant": 90, "user": 110}
    assert got["speaker_share_bp"] == {"assistant": 4500, "user": 5500}
    assert got["history_speaker_share_bp"] == {"assistant": 8900, "user": 1100}
    shown_as_row = speaker_share([unit("d", [{"role": "user", "content": "x" * 50}], index_row="row text")])
    assert shown_as_row["speaker_chars"] == {"index": 8}
    assert speaker_share([]) == {"speaker_chars": {}, "speaker_share_bp": {}}
    assert speaker_summary([{"memory": {"units": 3}}]) == {}             # rows of earlier runs: no summary entry
    rows = [{"memory": got}, {"memory": speaker_share(history[2:], history)}]
    summary = speaker_summary(rows)
    assert summary["pooled_share_bp"] == {"assistant": 8900, "user": 1100}
    assert summary["mean_share_bp"] == {"assistant": 7250, "user": 2750}


def test_longmemeval_and_beam_rows_carry_speaker_share(tmp_path: Path):
    ctx = _ctx(tmp_path, TelarchMemory(tmp_path / "work"), FakeClient("reader", "It is a beagle."),
               FakeClient("gpt-4o-2024-08-06", "yes"))
    out = bench_longmemeval.run(ctx, data_path=_lme_data(tmp_path), official=FakeLongMemEval())
    rows = {r["id"]: r for r in map(json.loads, (out / "judgments.jsonl").read_text().splitlines())}
    row = rows["q1"]                    # rows land in completion order; q2_abs matches nothing and packs nothing
    assert rows["q2_abs"]["memory"]["speaker_chars"] == {}
    assert set(row["memory"]["speaker_chars"]) == {"assistant", "user"}
    assert sum(row["memory"]["speaker_share_bp"].values()) == 10000
    assert "speaker_share" in json.loads((out / "summary.json").read_text())

    turns = [[{"role": "user", "content": "I am moving to Lisbon in July.", "time_anchor": "March-15-2024"},
              {"role": "assistant", "content": "Lisbon is lovely in summer."}]]
    conv = tmp_path / "beam" / "1"
    (conv / "probing_questions").mkdir(parents=True)
    (conv / "chat.json").write_text(json.dumps([{"batch_number": 1, "turns": turns}]))
    (conv / "probing_questions" / "probing_questions.json").write_text(json.dumps({
        "abstention": [{"question": "Where am I moving?", "rubric": ["Lisbon"]}]}))
    ctx = _ctx(tmp_path / "b", TelarchMemory(tmp_path / "b" / "work"), FakeClient("reader", "Lisbon"),
               FakeClient("gpt-4.1-mini", "{}"))
    out = bench_beam.run(ctx, data_dir=tmp_path / "beam", official=FakeBeam(), abilities=["abstention"])
    row = json.loads((out / "judgments.jsonl").read_text().splitlines()[0])
    assert row["memory"]["speaker_chars"] == {"assistant": 27, "user": 30} and len(row["prompt_sha256"]) == 64


@pytest.mark.parametrize("text", [
    "The agenda for the festival is not provided in the context.",
    "I'm sorry, but I don't have information about Alexis's reaction.",
    "There is no mention of that activity in the chat history.",
    "I cannot determine what you baked for your uncle's birthday party.",
    "That detail was never mentioned.",
    "The history does not specify which framework was used.",
])
def test_declining_answers_are_recognised(text: str):
    assert abstention_audit.declines(text)


@pytest.mark.parametrize("text", [
    "The atmosphere during the exhibit was vibrant and engaging.",
    "1. Introduction to Angle and Side Notation 2. Practice Problems",
    "You have added **15 autographed baseballs** to your collection.",
    "The virtual therapy sessions with Sara are conducted via Zoom.",
])
def test_answers_that_supply_the_detail_are_not(text: str):
    assert not abstention_audit.declines(text)


def test_abstention_audit_cross_tabulates_credit_with_declining(tmp_path: Path):
    def beam_row(i, ability, score, answer):
        return {"id": f"100K/1/{ability}/{i}", "ability": ability, "score": score, "question": f"q{i}?",
                "answer": answer, "evaluation": {"llm_judge_responses": [{"score": score, "reason": f"reason {i}"}]}}

    run = _run(tmp_path, "beam", "beam", [
        beam_row(0, "abstention", 1.0, "That is not provided in the context."),
        beam_row(1, "abstention", 1.0, "The atmosphere was vibrant and engaging."),       # credited, did not decline
        beam_row(2, "abstention", 0.0, "The sessions are conducted via Zoom."),
        beam_row(3, "abstention", 0.0, "I don't have information about that."),          # declined, not credited
        beam_row(4, "abstention", 0.5, "It was lively."),
        beam_row(5, "information_extraction", 1.0, "March 29"),
    ])
    out = abstention_audit.audit(run)
    assert out["abstention_questions"] == 5 and out["mean_score_bp"] == 5000 and out["declining_by_phrase"] == 2
    assert out["cells"] == {"credited_declining": 1, "credited_not_declining": 1, "partial_declining": 0,
                            "partial_not_declining": 1, "uncredited_declining": 1, "uncredited_not_declining": 1}
    assert [r["id"].rsplit("/", 1)[1] for r in out["rows"] if r["suspect"]] == ["1", "3", "4"]
    text = abstention_audit.render(out)
    assert "100K/1/abstention/1" in text and "reason 1" in text and "100K/1/abstention/0" not in text

    lme = _run(tmp_path, "lme", "longmemeval", [
        {"id": "a_abs", "hypothesis": "You have 15 baseballs.", "autoeval_label": {"label": True}, "judge_reply": "yes"},
        {"id": "b_abs", "hypothesis": "There is no mention of a cat.", "autoeval_label": {"label": True},
         "judge_reply": "yes"},
        {"id": "c", "hypothesis": "beagle", "autoeval_label": {"label": True}, "judge_reply": "yes"},
    ])
    out = abstention_audit.audit(lme, {"a_abs": {"question": "How many footballs?", "gold": "not enough information"}})
    assert out["abstention_questions"] == 2 and out["suspects"] == 1
    assert "GOLD: not enough information" in abstention_audit.render(out)
    with pytest.raises(SystemExit):
        abstention_audit.audit(_run(tmp_path, "loc", "locomo", []))
