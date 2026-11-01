"""A cap on packed items (telarch phase-P11, item P11.1).

--max-items N (or --max-items-file, a JSON object of history id -> N; LongMemEval's history id is the question id)
keeps, after packing and after the render fit, the N packed items of highest packing relevance (ties: the unit's
rank, then order) and drops the rest. A capped run therefore packs a subset of the uncapped run's items, and its
prompt shrinks with them. It lets a packing arm be read at another arm's item count: P11 reads --pair-window 192
--scoring mix at the control's item count, question by question. Unset, nothing changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telarch_evals import bench_longmemeval
from telarch_evals.common import Context
from telarch_evals.llm import Meter
from telarch_evals.memory import TelarchMemory

DATE = "2023/06/01 (Thu) 10:00"
QUESTION = "Which hiking trail by the lake did I enjoy most?"

# The config keys of the telarch system as telarch-benchmarks 325c393 wrote them.
CONFIG_KEYS_325C393 = [
    "system", "budget_tokens", "budget_scope", "candidates", "per_term_limit", "harpe_policy", "harpe_token_counter",
    "kibisis_encrypted", "kibisis_memory_security", "kibisis_cache_mb", "kibisis_search", "kibisis_env_passphrase",
    "kibisis_admission_gate", "query_planner", "stopwords_sha256", "fill_floor", "passthrough_under_tokens", "recency",
    "mmr", "bridge", "resolve_relative_dates", "surface_conflicts"]


class FakeClient:
    def __init__(self, model: str, reply: str):
        self.model, self.reply, self.calls = model, reply, []
        self.cache = None

    def describe(self) -> dict:
        return {"model": self.model}

    def complete(self, messages, *, max_tokens=None, response_format=None):
        self.calls.append(messages)
        return SimpleNamespace(text=self.reply, model=self.model, usage={"prompt_tokens": 3, "completion_tokens": 1},
                               cached=False)


class FakeLongMemEval:
    reader_template_cot = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
    reader_template_plain = "History:{}\nCurrent Date: {}\nQuestion: {}\nAnswer:"

    def anscheck_prompt(self, task, question, answer, response, abstention=False):
        return f"gold={answer} response={response} abstention={abstention}"

    def judge_model(self, name):
        return name


class Spy(TelarchMemory):
    """TelarchMemory that remembers the last pack it chose from and counts its recalls."""

    recalls = 0

    def _chosen(self, report, by_uid, question):
        self.last_report = report
        return super()._chosen(report, by_uid, question)

    def recall(self, history_id, question, render=None):
        type(self).recalls += 1
        return super().recall(history_id, question, render)


TRAILS = ["Cedar Ridge", "Falcon Loop", "Mirror Lake", "Old Mill", "Pine Hollow", "Quarry Rim"]


def _entry(qid: str) -> dict:
    """Six sessions of two rounds; the rounds use the question's words more or less often, so the packed items
    differ in relevance, and each round's reply is long enough to be cut into windows under --pair-window 192."""
    sessions = []
    for s, trail in enumerate(TRAILS):
        rounds = []
        for r in range(2):
            mentions = " ".join(["hiking trail by the lake"] * (1 + (s + r) % 4))
            user = f"Session {s} round {r}: I walked the {trail} {mentions} today and took notes."
            reply = " ".join(f"Note {s}-{r}-{k}: the {trail} path passes stone marker {k} and a bench." for k in range(25))
            rounds += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        sessions.append(rounds)
    sessions[2][0]["has_answer"] = True
    ids = [f"{qid}_s{i}" for i in range(len(sessions))]
    return {"question_id": qid, "question_type": "single-session-user", "question": QUESTION, "answer": "Mirror Lake",
            "question_date": DATE, "haystack_session_ids": ids,
            "haystack_dates": [f"2023/05/{i + 1:02d} (Mon) 09:00" for i in range(len(sessions))],
            "haystack_sessions": sessions, "answer_session_ids": [ids[2]]}


def _recall(mem: TelarchMemory, entry: dict):
    mem.open(entry["question_id"], bench_longmemeval.units_for(entry))
    try:
        return mem.recall(entry["question_id"], entry["question"], bench_longmemeval.render_history)
    finally:
        mem.close(entry["question_id"])


def _uncapped(tmp_path: Path, entry: dict):
    spy = Spy(tmp_path / "uncapped", pair_window=192, scoring="mix")
    recall = _recall(spy, entry)
    meta = {i.id: i.metadata for i in spy.last_report.included_items}
    return recall, meta


def test_a_cap_keeps_the_n_packed_items_of_highest_relevance(tmp_path):
    entry = _entry("cap")
    full, meta = _uncapped(tmp_path, entry)
    n = 4
    assert len(full.units) > n + 2, "the fixture no longer packs enough items to cap"
    assert len({round(meta[u.uid]["relevance"], 9) for u in full.units}) > 2, "the packed items tie on relevance"
    position = {u.uid: k for k, u in enumerate(full.units)}
    best = sorted(full.units, key=lambda u: (-meta[u.uid]["relevance"], meta[u.uid]["rank"], position[u.uid]))[:n]
    capped = _recall(TelarchMemory(tmp_path / "capped", pair_window=192, scoring="mix", max_items=n), entry)
    assert [u.uid for u in capped.units] == [u.uid for u in sorted(best, key=lambda u: u.order)]
    assert {u.uid for u in capped.units} <= {u.uid for u in full.units}
    assert capped.stats["selected"] == n and capped.stats["max_items"] == n
    assert capped.stats["capped"] == len(full.units) - n
    assert capped.stats["joined_tokens_o200k"] < full.stats["joined_tokens_o200k"], "the prompt did not shrink"
    assert capped.stats["context_tokens"] < full.stats["context_tokens"]


def test_a_cap_at_or_above_the_packed_count_changes_nothing(tmp_path):
    entry = _entry("above")
    full, _ = _uncapped(tmp_path, entry)
    for n in (len(full.units), len(full.units) + 5):
        capped = _recall(TelarchMemory(tmp_path / f"cap{n}", pair_window=192, scoring="mix", max_items=n), entry)
        assert [u.uid for u in capped.units] == [u.uid for u in full.units]
        assert bench_longmemeval.render_history(capped.units) == bench_longmemeval.render_history(full.units)
        assert capped.stats["capped"] == 0
        assert {k: v for k, v in capped.stats.items() if not k.endswith("_ms") and k not in ("max_items", "capped")} \
            == {k: v for k, v in full.stats.items() if not k.endswith("_ms")}


def test_with_no_cap_the_config_and_the_stats_are_the_harness_before(tmp_path):
    assert list(TelarchMemory(tmp_path / "a").config()) == CONFIG_KEYS_325C393
    assert list(TelarchMemory(tmp_path / "b", pair_window=192, scoring="mix").config()) \
        == CONFIG_KEYS_325C393 + ["pair_window", "scoring"]
    full, _ = _uncapped(tmp_path, _entry("plain"))
    assert "max_items" not in full.stats and "capped" not in full.stats


def _write(path: Path, caps: dict) -> Path:
    path.write_text(json.dumps(caps, indent=1) + "\n")
    return path


def test_the_cap_is_recorded_in_the_config_when_set(tmp_path):
    assert TelarchMemory(tmp_path / "n", max_items=7).config()["max_items"] == 7
    caps = _write(tmp_path / "caps.json", {"q1": 3, "q2": 5})
    cfg = TelarchMemory(tmp_path / "f", pair_window=192, scoring="mix", max_items_file=caps).config()
    blob = caps.read_bytes()
    assert cfg["max_items_file"] == {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob), "histories": 2}
    assert list(cfg)[:-1] == CONFIG_KEYS_325C393 + ["pair_window", "scoring"]


def _run(tmp_path: Path, system: TelarchMemory, *entries: dict, root: str = "runs"):
    data = tmp_path / "lme.json"
    data.write_text(json.dumps(list(entries)))
    reader = FakeClient("reader", "an answer")
    ctx = Context(runs_root=tmp_path / root, workdir=system.workdir, system=system, reader=reader,
                  judge=FakeClient("judge", "no"), meter=Meter(), workers=1, retrieval_only=True)
    return bench_longmemeval.run(ctx, data_path=data, official=FakeLongMemEval()), reader


def test_a_file_caps_each_question_at_its_own_count_and_the_manifest_binds_it(tmp_path):
    a, b = _entry("qa"), _entry("qb")
    caps = _write(tmp_path / "caps.json", {"qa": 2, "qb": 5})
    out, _ = _run(tmp_path, TelarchMemory(tmp_path / "w", pair_window=192, scoring="mix", max_items_file=caps), a, b)
    rows = {json.loads(x)["id"]: json.loads(x) for x in (out / "judgments.jsonl").read_text().splitlines() if x}
    assert {q: rows[q]["memory"]["selected"] for q in rows} == {"qa": 2, "qb": 5}
    assert {q: rows[q]["memory"]["max_items"] for q in rows} == {"qa": 2, "qb": 5}
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["config"]["memory"]["max_items_file"]["sha256"] == hashlib.sha256(caps.read_bytes()).hexdigest()


def test_a_question_missing_from_the_file_is_an_error_before_any_recall(tmp_path):
    Spy.recalls = 0
    caps = _write(tmp_path / "caps.json", {"qa": 2})
    system = Spy(tmp_path / "w", pair_window=192, scoring="mix", max_items_file=caps)
    with pytest.raises(ValueError, match="qb"):
        _run(tmp_path, system, _entry("qa"), _entry("qb"))
    assert Spy.recalls == 0


@pytest.mark.parametrize("knob", [{"surface_conflicts": True}, {"passthrough_under": 4000}])
def test_the_cap_is_rejected_where_packed_items_do_not_define_the_context(tmp_path, knob):
    caps = _write(tmp_path / "caps.json", {"q": 3})
    with pytest.raises(ValueError, match="packed items"):
        TelarchMemory(tmp_path / "n", max_items=3, **knob)
    with pytest.raises(ValueError, match="packed items"):
        TelarchMemory(tmp_path / "f", max_items_file=caps, **knob)


@pytest.mark.parametrize("caps", [{"q": 0}, {"q": -1}, {"q": 2.5}, {"q": True}, {"q": "3"}, ["q", 3], {}])
def test_a_cap_file_holds_positive_whole_numbers(tmp_path, caps):
    with pytest.raises(ValueError):
        TelarchMemory(tmp_path / "f", max_items_file=_write(tmp_path / "caps.json", caps))


def test_one_cap_at_a_time(tmp_path):
    caps = _write(tmp_path / "caps.json", {"q": 3})
    with pytest.raises(ValueError):
        TelarchMemory(tmp_path / "both", max_items=3, max_items_file=caps)
    with pytest.raises(ValueError):
        TelarchMemory(tmp_path / "neg", max_items=-2)


def test_the_cli_hands_the_cap_to_the_telarch_system(tmp_path, monkeypatch):
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
    caps = _write(tmp_path / "caps.json", {"q": 3})
    args = ["longmemeval", "--api-key-env", "NONE", "--retrieval-only", "--runs-dir", str(tmp_path / "runs")]
    assert cli.main(args + ["--pair-window", "192", "--max-items-file", str(caps)]) == 0
    assert cli.main(args + ["--max-items", "4"]) == 0
    assert cli.main(args) == 0
    assert seen[0].system.config()["max_items_file"]["histories"] == 1
    assert seen[1].system.config()["max_items"] == 4
    assert "max_items" not in seen[2].system.config() and "max_items_file" not in seen[2].system.config()
    with pytest.raises(SystemExit):
        cli.main(args + ["--max-items", "4", "--system", "full-context"])
