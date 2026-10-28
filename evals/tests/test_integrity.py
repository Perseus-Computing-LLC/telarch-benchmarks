"""Harness integrity: spend that survives a resume, a budget that covers the rendered context, and a response
cache whose first stored answer wins."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_offline import _history

import telarch_evals
from telarch_evals import (
    bench_beam,
    bench_locomo,
    bench_longmemeval,
    bench_memoryagentbench,
)
from telarch_evals.llm import Cache, ChatClient, Meter
from telarch_evals.memory import FullContextMemory, TelarchMemory, Unit, _o200k_counter
from telarch_evals.runlog import RunLog, execute, tombstone, verify

QUESTION = "Which notes mention tomatoes and gardening?"
RENDERS = [bench_beam.render, bench_locomo.render, bench_longmemeval.render_history, bench_memoryagentbench.memories]


def _units(n: int = 300):
    return [replace(u, turns=[{"role": "user", "content": u.text}]) for u in _history(n)]


def _recall(system, render=None):
    system.open("h", _units())
    try:
        return system.recall("h", QUESTION, render)
    finally:
        system.close("h")


def test_max_usd_caps_a_resumed_run_as_a_whole(tmp_path: Path):
    runs, config = tmp_path / "runs", {"system": "telarch", "benchmark": "longmemeval"}

    def process(ids: list[str], max_usd: float | None, resume: Path | None = None):
        meter = Meter({"m": (1.0, 0.0)})  # $1 per million prompt tokens: each call costs $0.40
        log = RunLog(runs, "longmemeval", config, sources=[], resume=resume)

        def work(job_id, _):
            meter.add("m", 400_000, 0, cached=False)
            return {"id": job_id}

        stop = execute(log, ((i, None) for i in ids), work, workers=1, meter=meter, max_usd=max_usd)
        log.finish({"rows": len(log.done), "stopped": stop})
        return log, meter, stop

    first, _, stop = process(["q1", "q2", "q3"], None)
    assert stop is None and json.loads((first.dir / "spend.json").read_text())["estimated_usd"] == 1.2
    # A new process starts with the run's $1.20 already spent, so a $1 cap stops it before any call.
    resumed, meter, stop = process(["q4", "q5"], 1.0, resume=first.dir)
    assert stop and "--max-usd 1.0" in stop and resumed.done == {"q1", "q2", "q3"}
    spend = json.loads((first.dir / "spend.json").read_text())
    assert spend["by_model"]["m"]["requests"] == meter.snapshot()["by_model"]["m"]["requests"] == 3
    assert spend["usd_per_million_tokens"] == {"m": [1.0, 0.0]} and spend["price_table_date"]
    assert verify(runs)["valid"]
    (first.dir / "spend.json").write_text(json.dumps({**spend, "estimated_usd": 0.0}))
    report = verify(runs)
    assert not report["valid"] and report["runs"][-1]["mismatched"] == ["spend.json"]


def test_a_meter_moved_to_another_run_starts_from_that_run(tmp_path: Path):
    meter = Meter()
    meter.track(tmp_path / "a.json")
    meter.add("m", 10, 1, cached=False)
    meter.track(tmp_path / "b.json")
    meter.add("m", 0, 0, cached=True)
    assert json.loads((tmp_path / "b.json").read_text())["by_model"]["m"] == {
        "requests": 0, "cache_hits": 1, "prompt_tokens": 0, "completion_tokens": 0}


def test_the_first_stored_response_wins(tmp_path: Path):
    cache = Cache(tmp_path / "c.sqlite")
    assert cache.put("k", {"text": "a"}) == {"text": "a"}
    assert cache.put("k", {"text": "b"}) == {"text": "a"} == cache.get("k")


def test_a_second_run_on_a_cache_in_use_stops_with_a_clear_error(tmp_path: Path):
    path = tmp_path / "c.sqlite"
    code = "import pathlib, sys; from telarch_evals.llm import Cache; Cache(pathlib.Path(sys.argv[1]))"
    probe = [sys.executable, "-c", code, str(path)]
    env = {**os.environ, "PYTHONPATH": str(Path(telarch_evals.__file__).parents[1])}
    held = Cache(path)
    second = subprocess.run(probe, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert second.returncode == 1 and f"in use by another run (pid {os.getpid()})" in second.stderr
    held.close()
    subprocess.run(probe, capture_output=True, env=env, timeout=60, check=True)  # free again once closed


def test_a_call_that_loses_a_race_answers_with_the_stored_response(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    cache = Cache(tmp_path / "c.sqlite")  # one run: its worker threads share the cache

    def client(answer: str) -> ChatClient:
        c = ChatClient("m", api_key_env="TELARCH_TEST_KEY", cache=cache, meter=Meter())
        c._post = lambda body: {"model": "m", "choices": [{"message": {"content": answer}}],
                                "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
        return c

    first, late, third = client("first"), client("late"), client("third")
    assert first.complete("q").text == "first"
    with monkeypatch.context() as m:
        m.setattr(cache, "get", lambda key: None)  # it looked before the first call had stored
        out = late.complete("q")
    assert (out.text, out.cached) == ("first", True) and late.meter.by_model["m"]["requests"] == 1
    assert third.complete("q").text == "first" and third.meter.by_model["m"]["cache_hits"] == 1


@pytest.mark.parametrize("render", RENDERS, ids=lambda r: f"{r.__module__.rsplit('.', 1)[-1]}.{r.__name__}")
def test_the_budget_covers_the_rendered_context(tmp_path: Path, render):
    count = _o200k_counter()
    loose = _recall(TelarchMemory(tmp_path / "a", budget=400))
    assert loose.stats["context_tokens"] <= 400 and "joined_tokens_o200k" not in loose.stats  # unchanged without
    fit = _recall(TelarchMemory(tmp_path / "b", budget=400), render)
    assert fit.units and fit.stats["joined_tokens_o200k"] == count(render(fit.units)) <= 400
    if count(render(loose.units)) > 400:
        assert fit.stats["fit_reserved"] > 0 and fit.stats["context_tokens"] < loose.stats["context_tokens"]
    assert fit.stats["context_tokens"] == sum(count(u.rendered) for u in fit.units)
    full = _recall(FullContextMemory(budget=400), render)
    newest = [u.uid for u in _units()][-len(full.units):]
    assert full.stats["joined_tokens_o200k"] == count(render(full.units)) <= 400
    assert [u.uid for u in full.units] == newest


def test_session_headers_no_longer_push_the_context_over(tmp_path: Path):
    # LongMemEval's rendering adds a dated header per session: on these units the old fit ran far over.
    count = _o200k_counter()
    loose = _recall(TelarchMemory(tmp_path / "a", budget=400))
    assert count(bench_longmemeval.render_history(loose.units)) > 450
    fit = _recall(TelarchMemory(tmp_path / "b", budget=400), bench_longmemeval.render_history)
    assert fit.stats["joined_tokens_o200k"] <= 400 and fit.stats["fit_reserved"] > 50


def test_window_packs_drop_whole_messages_when_the_rendering_runs_over(tmp_path: Path):
    # Headers alone outweigh the budget, so packing again cannot help: the least relevant messages go instead.
    count = _o200k_counter()

    def render(units):
        return "\n\n".join("note " * 150 + u.rendered for u in units)

    fit = _recall(TelarchMemory(tmp_path, budget=400, window=128, encrypt=False), render)
    assert fit.stats["fit_dropped"] > 0 and fit.units and all("#" in u.uid for u in fit.units)
    assert fit.stats["joined_tokens_o200k"] == count(render(fit.units)) <= 400
    assert fit.stats["context_tokens"] == sum(count(u.rendered) for u in fit.units)


def test_random_histories_never_exceed_the_budget_and_fitting_packs_do_not_change(tmp_path: Path):
    rng, count = random.Random(20261024), _o200k_counter()
    words = ["tomatoes", "gardening", "weather", "beagle", "Biscuit", "seeds", "rain", "harvest", "notes",
             "the", "a", "of"]
    question = "Where did the beagle Biscuit find the tomatoes after the rain?"
    for case in range(12):
        session, units = 0, []
        for i in range(rng.randint(20, 150)):
            session += rng.random() < 0.2
            date = f"2023/05/{1 + session % 28:02d}"
            text = "user: " + " ".join(rng.choices(words, k=rng.randint(1, 120)))
            units.append(Unit(uid=f"{case}:{i}", session=f"s{session}", order=(session, i), date=date, text=text,
                              rendered=f"[{date}]\n{text}", turns=[{"role": "user", "content": text}]))
        budget, render = rng.choice([120, 400, 1500]), rng.choice(RENDERS)
        telarch = TelarchMemory(tmp_path / str(case), budget=budget, encrypt=False)
        windowed = TelarchMemory(tmp_path / f"w{case}", budget=budget, encrypt=False, window=128)
        for system in (telarch, windowed, FullContextMemory(budget=budget)):
            system.open("h", units)
            try:
                loose, fit = system.recall("h", question), system.recall("h", question, render)
            finally:
                system.close("h")
            where = (case, system.name, getattr(system, "window", 0), render.__name__, budget)
            assert fit.stats["joined_tokens_o200k"] == count(render(fit.units)) <= budget, where
            if count(render(loose.units)) <= budget:
                assert [u.uid for u in fit.units] == [u.uid for u in loose.units], where


def test_a_tombstoned_run_is_listed_not_compared(tmp_path: Path):
    import shutil
    import pytest
    runs = tmp_path / "runs"
    log = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam"}, sources=[])
    log.finish({"rows": 0, "complete": True})
    assert verify(runs)["valid"]
    with pytest.raises(RuntimeError, match="still on disk"):
        tombstone(runs, "beam", log.dir.name, "smoke run")
    shutil.rmtree(log.dir)
    assert not verify(runs)["valid"]
    with pytest.raises(RuntimeError, match="no eval.run.completed"):
        tombstone(runs, "beam", "never-ran", "smoke run")
    with pytest.raises(RuntimeError, match="reason"):
        tombstone(runs, "beam", log.dir.name, "  ")
    event = tombstone(runs, "beam", log.dir.name, "20-row smoke canary, removed on purpose")
    report = verify(runs)
    assert report["valid"] and report["chain_valid"]
    entry = next(r for r in report["runs"] if r["run_id"] == log.dir.name)
    assert entry["tombstoned"] == event["sequence"] and "files_match" not in entry


def test_a_run_directory_the_ledger_never_completed_is_unlisted(tmp_path: Path):
    runs = tmp_path / "runs"
    done = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam"}, sources=[])
    done.finish({"rows": 0, "complete": True})
    assert verify(runs)["valid"] and verify(runs)["unlisted"] == []
    # A run that started but never finished: its directory exists, no completion pins its files.
    started = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam", "x": 1}, sources=[])
    started._fh.close()
    report = verify(runs)
    assert not report["valid"] and report["chain_valid"]
    assert report["unlisted"] == [{"run_id": started.dir.name, "benchmark": "beam", "started_in_ledger": True}]
    # A directory copied in from elsewhere: no event of any kind names it.
    foreign = runs / "locomo" / "20260101T000000Z-telarch-deadbeef"
    foreign.mkdir(parents=True)
    (foreign / "summary.json").write_text("{}")
    report = verify(runs)
    assert not report["valid"]
    assert {(u["benchmark"], u["run_id"], u["started_in_ledger"]) for u in report["unlisted"]} == {
        ("beam", started.dir.name, True), ("locomo", foreign.name, False)}
    # Finishing the started run and removing the foreign directory makes the tree verifiable again.
    started._fh = open(started.rows_path, "a", encoding="utf-8")
    started.finish({"rows": 0, "complete": True})
    import shutil
    shutil.rmtree(foreign)
    assert verify(runs)["valid"]


def test_a_completion_with_no_file_digests_does_not_verify(tmp_path: Path):
    from telarch_evals.runlog import ACTOR, _ledger
    runs = tmp_path / "runs"
    log = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam"}, sources=[])
    log.finish({"rows": 0, "complete": True})
    assert verify(runs)["valid"]
    # A later completion of the same run that pins nothing: the ledger accepts the event (it is
    # append-only and does not know the schema), but verify must not treat "nothing to compare" as a pass.
    for files in ({}, None):
        payload = {"run_id": log.dir.name, "benchmark": "beam", "rows": 0, "complete": True}
        if files is not None:
            payload["files_sha256"] = files
        _ledger(runs).append("eval.run.completed", ACTOR, payload)
        report = verify(runs)
        assert not report["valid"] and report["chain_valid"]
        bad = report["runs"][-1]
        assert bad["files_match"] is False and bad["invalid_completion"] == "files_sha256 is empty"
        # The earlier, complete completion of the same run still verifies on its own.
        assert report["runs"][0]["files_match"] is True


def _fake_client(cache: Cache, answer: str, **kwargs) -> ChatClient:
    c = ChatClient("m", api_key_env="TELARCH_TEST_KEY", cache=cache, meter=Meter(), **kwargs)
    c._post = lambda body: {"model": "m", "choices": [{"message": {"content": answer}}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
    return c


def test_sampled_requests_are_cached_per_run_and_greedy_ones_across_runs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    cache = Cache(tmp_path / "c.sqlite")
    # temperature 0: the same request in a later run is the same measurement, so it may be replayed.
    assert _fake_client(cache, "first").complete("q").text == "first"
    greedy = _fake_client(cache, "second")
    assert greedy.complete("q").text == "first" and greedy.meter.by_model["m"]["cache_hits"] == 1
    assert "cache_salt" not in greedy.describe()
    # temperature 0.7: a new run gets a new salt, so it draws again instead of replaying the first run.
    run1 = _fake_client(cache, "draw-1", temperature=0.7)
    run2 = _fake_client(cache, "draw-2", temperature=0.7)
    assert run1.complete("q").text == "draw-1" and run2.complete("q").text == "draw-2"
    assert run1.cache_salt != run2.cache_salt and run2.meter.by_model["m"] == {
        "requests": 1, "cache_hits": 0, "prompt_tokens": 5, "completion_tokens": 1}
    assert run1.describe()["cache_salt"] == run1.cache_salt
    # Within one run the cache still works: a retry of the same request answers with the stored draw.
    retry = _fake_client(cache, "draw-3", temperature=0.7, cache_salt=run1.cache_salt)
    assert retry.complete("q").text == "draw-1" and retry.meter.by_model["m"]["cache_hits"] == 1
    # A derived client (same run, another model setting) shares the salt.
    assert run1.derive("m", temperature=0.5).cache_salt == run1.cache_salt


def test_the_manifest_records_cache_hits_and_misses(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELARCH_TEST_KEY", "test")
    cache = Cache(tmp_path / "c.sqlite")
    runs = tmp_path / "runs"
    log = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam"}, sources=[])
    client = _fake_client(cache, "a")
    client.meter.track(log.dir / "spend.json")
    client.complete("q1")
    client.complete("q1")  # a hit
    client.complete("q2")
    log.finish({"rows": 2, "complete": True})
    manifest = json.loads((log.dir / "manifest.json").read_text())
    assert manifest["cache"] == {"hits": 1, "misses": 2, "by_model": {"m": {"hits": 1, "misses": 2}}}
    # The completion event pins the manifest as rewritten, so verify still passes.
    assert verify(runs)["valid"]
    # A run that never tracked spend has no cache section rather than a made-up zero.
    bare = RunLog(runs, "beam", {"system": "telarch", "benchmark": "beam", "x": 1}, sources=[])
    bare.finish({"rows": 0, "complete": True})
    assert "cache" not in json.loads((bare.dir / "manifest.json").read_text())
