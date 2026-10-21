"""Round-2 harness options: packing in reader tokens, the any-word planner, the candidate floor, passthrough of
small histories, recency, MMR and 2-hop bridge expansion, retrieval-only screens, and package sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from telarch_evals import bench_locomo, bench_longmemeval
from telarch_evals.common import Context, OfflineClient
from telarch_evals.llm import Meter
from telarch_evals.memory import FullContextMemory, TelarchMemory, Unit, _o200k_counter, make_system, package_sources
from test_offline import _history, _lme_data


def _units(texts: list[str]) -> list[Unit]:
    return [Unit(uid=f"0:{i}", session=f"s{i}", order=(i,), date="2023/05/01", text=t, rendered=f"[2023/05/01]\n{t}")
            for i, t in enumerate(texts)]


def _recall(mem: TelarchMemory, units: list[Unit], question: str):
    mem.open("h", units)
    try:
        return mem.recall("h", question)
    finally:
        mem.close("h")


def test_packing_counts_reader_tokens_by_default(tmp_path: Path):
    count = _o200k_counter()
    mem = TelarchMemory(tmp_path / "a", budget=400)
    assert mem.config()["harpe_token_counter"] == "tiktoken o200k_base"
    recall = _recall(mem, _history(), "Which notes mention tomatoes and gardening?")
    stats = recall.stats
    assert stats["context_tokens"] == stats["context_tokens_o200k"] == sum(count(u.rendered) for u in recall.units)
    assert 0 < stats["context_tokens"] <= 400 and stats["context_tokens_heuristic"] > 0
    old = TelarchMemory(tmp_path / "b", budget=400, counter="heuristic")
    assert old.config()["harpe_token_counter"] == "HeuristicTokenCounter"
    stats = _recall(old, _history(), "Which notes mention tomatoes and gardening?").stats
    assert stats["context_tokens"] == stats["context_tokens_heuristic"]
    with pytest.raises(ValueError):
        TelarchMemory(tmp_path / "c", counter="words")


@pytest.mark.parametrize("encrypt", [True, False])
def test_any_planner_finds_the_needle_with_one_search(tmp_path: Path, encrypt: bool):
    mem = TelarchMemory(tmp_path, budget=300, planner="any", encrypt=encrypt)
    assert "match='any'" in mem.config()["query_planner"]
    recall = _recall(mem, _history(), "What is the name of the dog I adopted?")
    assert "0:77" in [u.uid for u in recall.units]
    assert recall.stats["searches"] == 1
    fanout = _recall(TelarchMemory(tmp_path / "f", budget=300, encrypt=encrypt), _history(),
                     "What is the name of the dog I adopted?")
    assert fanout.stats["searches"] == 3 and "0:77" in [u.uid for u in fanout.units]


@pytest.mark.parametrize("planner", ["fanout", "any"])
def test_candidate_floor_widens_retrieval_until_the_budget_fills(tmp_path: Path, planner: str):
    texts = [f"user: tomato note {i} with a few extra words to take up some room" for i in range(80)]
    low = _recall(TelarchMemory(tmp_path / "a", budget=600, per_term=2, candidates=4, planner=planner),
                  _units(texts), "tomato note").stats
    high = _recall(TelarchMemory(tmp_path / "b", budget=600, per_term=2, candidates=4, planner=planner,
                                 fill_floor=0.9), _units(texts), "tomato note").stats
    assert low["widened"] == 0 and low["context_tokens"] < 0.9 * 600
    assert high["widened"] >= 1 and low["context_tokens"] < high["context_tokens"] <= 600


def test_candidate_floor_stops_when_retrieval_is_exhausted(tmp_path: Path):
    texts = ["user: one tomato note"] + [f"user: unrelated chatter {i}" for i in range(30)]
    stats = _recall(TelarchMemory(tmp_path, budget=600, fill_floor=0.9), _units(texts), "tomato").stats
    assert stats["widened"] == 0 and stats["selected"] == 1


def test_recall_searches_keys_only_and_never_opens_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kibisis import SQLiteMemoryVault

    def refuse(*args, **kwargs):
        raise AssertionError("the harness holds the text: search() would open (and decrypt) every result")

    monkeypatch.setattr(SQLiteMemoryVault, "search", refuse)
    for encrypt in (True, False):
        mem = TelarchMemory(tmp_path / str(encrypt), budget=400, encrypt=encrypt)
        assert mem.config()["kibisis_search"].endswith("(search_keys, 0.1.0a8+)")
        stats = _recall(mem, _history(), "Which notes mention tomatoes and gardening?").stats
        assert stats["selected"] > 0


def test_memory_security_setting_reaches_the_manifest(tmp_path: Path):
    assert TelarchMemory(tmp_path / "a").config()["kibisis_memory_security"] is True
    assert TelarchMemory(tmp_path / "b", memory_security=False).config()["kibisis_memory_security"] is False
    assert TelarchMemory(tmp_path / "c", encrypt=False).config()["kibisis_memory_security"] is None
    mem = make_system("telarch", tmp_path / "d", budget=None, candidates=10, per_term=5, policy="least_relevant",
                      admission_gate=False, encrypt=True, memory_security=False)
    assert mem.config()["kibisis_memory_security"] is False


def test_resolve_dates_changes_what_the_reader_sees_not_what_is_searched(tmp_path: Path):
    units = [Unit(uid="0:0", session="s0", order=(0,), date="11 May, 2023", text="user: I interviewed yesterday",
                  rendered="[11 May, 2023] user: I interviewed yesterday",
                  turns=[{"role": "user", "content": "I interviewed yesterday"}]),
             Unit(uid="0:1", session="s0", order=(1,), date="11 May, 2023", text="user: the tomato harvest",
                  rendered="[11 May, 2023] user: the tomato harvest")]
    mem = TelarchMemory(tmp_path / "a", budget=400, resolve_dates=True)
    assert mem.config()["resolve_relative_dates"] is True
    recall = _recall(mem, units, "When did I interview?")
    seen = {u.uid: u for u in recall.units}
    assert seen["0:0"].rendered == "[11 May, 2023] user: I interviewed yesterday [10 May 2023]"
    assert seen["0:0"].turns[0]["content"] == "I interviewed yesterday [10 May 2023]"
    plain = _recall(TelarchMemory(tmp_path / "b", budget=400), units, "When did I interview?")
    assert [u.uid for u in plain.units] == [u.uid for u in recall.units]  # retrieval is unchanged
    assert {u.uid: u for u in plain.units}["0:0"].rendered == units[0].rendered


def test_offline_clients_derive_offline_clients():
    judge = OfflineClient("judge")
    derived = judge.derive("gpt-4o-2024-05-13", temperature=0.1)
    assert isinstance(derived, OfflineClient) and derived.describe()["retrieval_only"] is True
    with pytest.raises(RuntimeError):
        derived.complete("prompt")


def test_surface_conflicts_notes_a_denial_of_a_selected_statement(tmp_path: Path):
    said = "user: I've written integration tests for socket events using Jest and socket.io-client."
    denied = "user: I've never written integration tests for any socket events, so where do I start?"
    units = [Unit(uid=f"0:{i}", session=f"s{i}", order=(i,), date=d, text=x, rendered=f"[{d}]\n{x}",
                  turns=[{"role": "user", "content": x.split(": ", 1)[1]}])
             for i, (d, x) in enumerate([("March-15-2024", said), ("April-05-2024", denied)])]
    question = "Have I ever written integration tests for socket events?"
    recall = _recall(TelarchMemory(tmp_path / "a", budget=400, surface_conflicts=True), units, question)
    seen = {u.uid: u for u in recall.units}
    assert recall.stats["conflict_notes"] == 1
    assert "[possible conflict: on March-15-2024, user said" in seen["0:1"].rendered
    assert "[possible conflict" in seen["0:1"].turns[0]["content"] and seen["0:0"].rendered == units[0].rendered
    plain = _recall(TelarchMemory(tmp_path / "b", budget=400), units, question)
    assert plain.stats["conflict_notes"] == 0 and [u.uid for u in plain.units] == [u.uid for u in recall.units]


def test_passthrough_hands_small_histories_over_like_full_context(tmp_path: Path):
    units = _history(30)
    mem = TelarchMemory(tmp_path / "a", passthrough_under=32_000)
    assert mem.config()["passthrough_under_tokens"] == 32_000
    recall = _recall(mem, units, "What is the name of the dog?")
    full = FullContextMemory(budget=100_000)
    full.open("h", units)
    whole = full.recall("h", "What is the name of the dog?")
    assert [u.uid for u in recall.units] == [u.uid for u in whole.units]
    assert recall.stats["passthrough"] is True and recall.stats["context_tokens"] == whole.stats["context_tokens"]
    small = _recall(TelarchMemory(tmp_path / "b", passthrough_under=50), units, "dog")
    assert small.stats["passthrough"] is False


def test_recency_prefers_the_newer_of_two_equal_matches(tmp_path: Path):
    units = _units(["user: my office is in building alpha"] + [f"user: filler {i}" for i in range(5)]
                   + ["user: my office is in building alpha"])
    by_uid = {u.uid: u for u in units}
    scores = {"0:0": 3.0, "0:6": 3.0}
    for recency, first in ((0.0, "0:0"), (0.5, "0:6")):
        mem = TelarchMemory(tmp_path / str(recency), recency=recency)
        mem.open("h", units)
        assert mem._rank("h", dict(scores), by_uid, 10)[0][0] == first
        mem.close("h")


def test_mmr_moves_a_near_duplicate_below_a_distinct_match(tmp_path: Path):
    units = _units(["user: alpha beta gamma delta", "user: alpha beta gamma delta", "user: epsilon zeta theta iota"])
    by_uid = {u.uid: u for u in units}
    scores = {"0:0": 10.0, "0:1": 9.9, "0:2": 9.0}
    for mmr, order in ((0.0, ["0:0", "0:1", "0:2"]), (0.5, ["0:0", "0:2", "0:1"])):
        mem = TelarchMemory(tmp_path / str(mmr), mmr=mmr)
        mem.open("h", units)
        assert [uid for uid, _ in mem._rank("h", dict(scores), by_uid, 10)] == order
        mem.close("h")


@pytest.mark.parametrize("planner", ["fanout", "any"])
def test_bridge_follows_a_name_to_a_second_hop(tmp_path: Path, planner: str):
    texts = ["user: Alice's sister is called Beth.", "user: Beth moved to Oslo last year."]
    texts += [f"user: unrelated chatter number {i}" for i in range(20)]
    question = "Which city does Alice's sister live in?"
    plain = _recall(TelarchMemory(tmp_path / "a", planner=planner), _units(texts), question)
    hop = _recall(TelarchMemory(tmp_path / "b", planner=planner, bridge=3), _units(texts), question)
    assert "0:1" not in [u.uid for u in plain.units]
    assert "0:1" in [u.uid for u in hop.units] and hop.stats["bridge_terms"] == ["beth"]


def test_make_system_passes_round2_options(tmp_path: Path):
    system = make_system("telarch", tmp_path, budget=None, candidates=200, per_term=50, policy="least_relevant",
                         admission_gate=False, encrypt=True, planner="any", fill_floor=0.9, passthrough_under=32_000,
                         recency=0.2, mmr=0.3, bridge=2)
    config = system.config()
    assert (config["fill_floor"], config["passthrough_under_tokens"], config["recency"], config["mmr"],
            config["bridge"]) == (0.9, 32_000, 0.2, 0.3, 2)
    assert "0.1.0a7" in config["kibisis_search"]


def _screen_ctx(tmp_path: Path) -> Context:
    return Context(runs_root=tmp_path / "screens", workdir=tmp_path / "work", system=TelarchMemory(tmp_path / "work"),
                   reader=OfflineClient("reader"), judge=OfflineClient("judge"), meter=Meter(), workers=2,
                   retrieval_only=True)


def test_locomo_retrieval_only_screen_makes_no_model_calls(tmp_path: Path):
    sample = {"sample_id": "conv-1", "conversation": {
        "speaker_a": "Ann", "speaker_b": "Bo", "session_1_date_time": "1:00 pm on 8 May, 2023",
        "session_1": [{"speaker": "Ann", "dia_id": "D1:1", "text": "I ran a marathon in Boston last week."},
                      {"speaker": "Bo", "dia_id": "D1:2", "text": "Amazing!"}]},
        "qa": [{"question": "Which city did Ann run a marathon in?", "answer": "Boston", "evidence": ["D1:1"],
                "category": 4}]}
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]))
    out = bench_locomo.run(_screen_ctx(tmp_path), data_path=path)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["retrieval_only"] is True and summary["complete"] is True and summary["errors"] == 0
    assert summary["evidence_recall"] == 1.0 and summary["by_group"]["single-hop"] == {"n": 1, "evidence_recall": 1.0}
    assert summary["prompt_tokens_o200k"]["mean"] > 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["config"]["retrieval_only"] is True and manifest["config"]["reader"]["model"] is None
    assert manifest["package_sources"]["kibisis"]["version"]
    assert str(out).startswith(str(tmp_path / "screens"))


def test_longmemeval_retrieval_only_screen_reports_session_recall_by_type(tmp_path: Path):
    out = bench_longmemeval.run(_screen_ctx(tmp_path), data_path=_lme_data(tmp_path))
    summary = json.loads((out / "summary.json").read_text())
    assert summary["retrieval_only"] is True and summary["errors"] == 0 and summary["rows"] >= 1
    assert summary["session_recall"] is not None and summary["by_group"]
    assert all("session_recall" in g for g in summary["by_group"].values())
    with pytest.raises(RuntimeError):
        OfflineClient("reader").complete("hi")


def test_package_sources_name_version_and_origin():
    sources = package_sources()
    assert set(sources) >= {"kibisis", "harpe", "argus-ledger"}
    for name in ("kibisis", "harpe"):
        assert sources[name]["version"] and sources[name]["source"] in ("index", "vcs", "editable", "url")


def test_add_ingests_more_units_into_an_open_history(tmp_path: Path):
    units = _history(40)
    mem = TelarchMemory(tmp_path, budget=300, planner="any")
    mem.open("h", units[:20])
    assert "0:77" not in [u.uid for u in mem.recall("h", "What is the name of the dog I adopted?").units]
    added = mem.add("h", _history(120)[70:80])
    assert added["units"] == 10 and added["ingest_errors"] == 0
    assert "0:77" in [u.uid for u in mem.recall("h", "What is the name of the dog I adopted?").units]
    mem.close("h")
